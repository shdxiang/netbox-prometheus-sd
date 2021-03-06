#!/usr/bin/env python3

import sys
import os
import json
import argparse
import itertools
import netaddr
import logging
from urllib3.exceptions import RequestError

import pynetbox


class Discovery(object):
    # https://github.com/netbox-community/netbox/blob/master/netbox/dcim/choices.py
    STATUS_ACTIVE = 'active'

    def __init__(self, args):
        super(Discovery, self).__init__()
        self.args = args

    def run(self):
        self.netbox = pynetbox.api(self.args.url, token=self.args.token)
        if self.args.discovery == 'device':
            targets = self.discover_device()
        elif self.args.discovery == 'vm':
            targets = self.discover_vm()
        elif self.args.discovery == 'circuit':
            targets = self.discover_circuit()
        else:
            return

        temp_file = None
        if self.args.output == '-':
            output = sys.stdout
        else:
            temp_file = '{}.tmp'.format(self.args.output)
            output = open(temp_file, 'w')

        json.dump(targets, output, indent=4)
        output.write('\n')

        if temp_file:
            output.close()
            os.rename(temp_file, self.args.output)
        else:
            output.flush()

    def gen_targets(self, items):
        targets = []

        for item in items:
            if not item.custom_fields.get(self.args.custom_field):
                continue

            labels = {'__port__': str(self.args.port)}
            if getattr(item, 'name', None):
                labels['__meta_netbox_name'] = item.name
            else:
                labels['__meta_netbox_name'] = repr(item)

            if getattr(item, 'site', None):
                labels['__meta_netbox_pop'] = item.site.slug

            try:
                item_targets = json.loads(
                    item.custom_fields[self.args.custom_field])
            except ValueError as e:
                logging.exception(e)
                continue

            if not isinstance(item_targets, list):
                item_targets = [item_targets]

            for target in item_targets:
                target_labels = labels.copy()
                target_labels.update(target)
                if hasattr(item, 'primary_ip'):
                    address = item.primary_ip
                else:
                    address = item
                targets.append({'targets': ['%s:%s' % (str(netaddr.IPNetwork(
                    address.address).ip), target_labels['__port__'])], 'labels': target_labels})

        return targets

    def discover_device(self):
        devices = self.netbox.dcim.devices.filter(
            has_primary_ip=True, status=self.STATUS_ACTIVE)

        return self.gen_targets(devices)

    def discover_vm(self):
        vms = self.netbox.virtualization.virtual_machines.filter(
            has_primary_ip=True, status=self.STATUS_ACTIVE)

        return self.gen_targets(vms)

    def get_circuit_ip(self, circuit):
        logging.debug(f'get_circuit_ip: {circuit}')
        try:
            ta = self.netbox.circuits.circuit_terminations.get(
                circuit.termination_a.id)
            logging.debug(f'terminal A: {ta}')

            tz = self.netbox.circuits.circuit_terminations.get(
                circuit.termination_z.id)
            logging.debug(f'terminal Z: {tz}')
        except RequestError as e:
            logging.exception(e)
            return None, None

        ipa = self.get_terminal_a_ip(ta)
        logging.debug(f'circuit {circuit}: IP of terminal A: {ipa}')

        ipz = self.get_terminal_z_ip(tz)
        logging.debug(f'circuit {circuit}: IP of terminal Z: {ipz}')

        return ipa, ipz

    # Here return `primary_ip`, not real `terminal_a_ip`, prometheus will get metrics from this IP
    def get_terminal_a_ip(self, ta):
        cable = self.netbox.dcim.cables.get(ta.cable.id)
        logging.debug(
            f'cable of terminal-a {ta}: {cable}, cable id: {cable.id}')

        if hasattr(cable.termination_a.device, 'id'):
            device = cable.termination_a.device
        elif hasattr(cable.termination_b.device, 'id'):
            device = cable.termination_b.device
        else:
            return None

        device = self.netbox.dcim.devices.get(device.id)
        if hasattr(device, 'primary_ip'):
            return str(netaddr.IPNetwork(device.primary_ip.address).ip)
        else:
            return None

    def get_terminal_z_ip(self, tz):
        cable = self.netbox.dcim.cables.get(tz.cable.id)
        logging.debug(
            f'cable of terminal-z {tz}: {cable}, cable id: {cable.id}')

        if hasattr(cable.termination_a.device, 'id'):
            device = cable.termination_a.device
            interface = cable.termination_a.name
        elif hasattr(cable.termination_b.device, 'id'):
            device = cable.termination_b.device
            interface = cable.termination_b.name
        else:
            return None

        try:
            ip = self.netbox.ipam.ip_addresses.filter(
                device_id=device.id, interface=interface)
        except RequestError as e:
            logging.exception(e)
            return None

        return str(netaddr.IPNetwork(ip[0].address).ip)

    def discover_circuit(self):

        targets = []

        circuits = self.netbox.circuits.circuits.filter(
            status=self.STATUS_ACTIVE)

        for circuit in itertools.chain(circuits):
            if not circuit.custom_fields.get(self.args.custom_field):
                continue

            logging.debug(f'circuit: {circuit}')

            ipa, ipz = self.get_circuit_ip(circuit)
            if not ipa or not ipz:
                continue

            labels = {'__port__': str(self.args.port)}

            if getattr(circuit, 'cid', None):
                labels['__meta_netbox_name'] = circuit.cid
            else:
                labels['__meta_netbox_name'] = repr(circuit)

            labels['__meta_netbox_target'] = ipz

            logging.debug(labels)

            try:
                device_targets = json.loads(
                    circuit.custom_fields[self.args.custom_field])
            except ValueError as e:
                logging.exception(e)
                continue

            if not isinstance(device_targets, list):
                device_targets = [device_targets]

            for target in device_targets:
                target_labels = labels.copy()
                target_labels.update(target)
                targets.append({'targets': ['%s:%s' % (
                    ipa, target_labels['__port__'])], 'labels': target_labels})

        return targets


def main():
    format = "%(asctime)s %(filename)s [%(lineno)d][%(levelname)s] %(message)s"
    logging.basicConfig(level=logging.INFO, format=format)

    parser = argparse.ArgumentParser()
    parser.add_argument('-p', '--port', default=10000,
                        help='Default target port; Can be overridden using the __port__ label')
    parser.add_argument('-f', '--custom-field', default='prom_labels',
                        help='Netbox custom field to use to get the target labels')
    parser.add_argument('url', help='URL to Netbox')
    parser.add_argument('token', help='Authentication Token')
    parser.add_argument('output', help='Output file')

    parser.add_argument('-d', '--discovery', default='device',
                        help='Discovery type, default: device', choices=['device', 'vm', 'circuit'])
    args = parser.parse_args()

    discovery = Discovery(args)

    discovery.run()


if __name__ == '__main__':
    main()
