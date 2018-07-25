#!/usr/bin/python

from __future__ import print_function
import itertools
from jinja2 import Environment, FileSystemLoader
import os
import logging
import sys
import yaml

BASEDIR = os.path.join(os.getcwd(), 'templates')
OUTPUTDIR = os.path.join(os.getcwd(), 'output')

logger = logging.getLogger()
handler = logging.StreamHandler()
formatter = logging.Formatter('%(levelname)-8s %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.DEBUG)

class Inventory(object):
    def __init__(self, inventory, latencyfile):
        self._fname = inventory
        with open(inventory, 'r') as stream:
            try:
                self._inventory = yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                print(exc)
        self._flatency = latencyfile
        with open(latencyfile, 'r') as stream:
            try:
                self._latency = yaml.safe_load(stream)
            except yaml.YAMLError as exc:
                print(exc)

    def get_roles(self):
        roles = []
        for i in self._inventory.keys():
            if 'vars' not in self._inventory[i]:
                continue
            if 'role_name' in self._inventory[i]['vars']:
                roles.append(i)
        return roles

    def get_hosts(self):
        hosts = {}
        for i in self._inventory.keys():
            if 'hosts' not in self._inventory[i]:
                continue
            host = self._inventory[i]['hosts'].keys()
            if len(host) != 1:
                raise Exception("Expected to see only one host in %s:%s" % (i, host))
            hosts[i] = host[0]
        return hosts 

    def get_ip_host(self, ip):
        # FIXME if the ip is a VIP we always return the first host of the contoller role
        # This is fine in 99% of the cases
        if ip in self.get_all_vips():
            h = self.get_role_hosts('Controller')
            ret = sorted(h.keys())[0]
            return ret

        hosts = []
        for host in self.get_hosts():
            if ip in self.get_host_ips(host):
                hosts.append(host)
        if len(hosts) > 1:
            raise Exception("Multiple hosts %s for %s" % (hosts, ip))
        if len(hosts) == 1:
            return hosts[0]
        return None

    def get_all_vips(self):
        if 'vars' not in self._inventory['overcloud']:
            return []
        v = self._inventory['overcloud']['vars']
        vips = []
        for (vip_name, vip) in v.iteritems():
            vips.append(vip)
        return vips

    def get_role_hosts(self, role):
        children = self._inventory[role]['children'].keys()
        hosts = self.get_hosts()
        ret = {k: hosts[k] for k in children}
        return ret

    def get_host_role(self, host):
        ret = []
        for role in self.get_roles():
            if host in self.get_role_hosts(role):
                ret.append(role)
        if len(ret) > 1:
            raise Exception("We had %s in multiple roles %s" % (host, ret))
        if len(ret) == 1:
            return ret[0]
        return None

    def get_ip_role(self, ip):
        # FIXME if the ip is a VIP we always return the controller role
        # This is fine in 99% of the cases
        if ip in self.get_all_vips():
            return 'Controller'
        host = self.get_ip_host(ip)
        if host == None:
            return None
        return self.get_host_role(host)

    def get_host_ips(self, hostname):
        v = self._inventory[hostname]['vars']
        # likely the undercloud which has no enabled_networks
        if 'enabled_networks' not in v:
            return []
        networks = v['enabled_networks']
        ips = set()
        for i in networks:
            net_key = "%s_ip" % i
            ips.add(v[net_key])
        return ips

    def get_host_ip_network(self, hostname, network):
        v = self._inventory[hostname]['vars']
        # likely the undercloud which has no enabled_networks
        if 'enabled_networks' not in v:
            return None
        networks = v['enabled_networks']
        if network in networks:
            return v['%s_ip' % network]
        return None

    def get_roles_connections(self):
        combinations = itertools.combinations(self.get_roles(), 2)
        ret = []
        for i in combinations:
            if i:
                ret.append(i)
        return ret

    def get_default_latency(self):
        if 'DefaultLatency' in self._latency:
            return self._latency['DefaultLatency']
        return None 

    def get_intra_role_latency(self, role):
        logger.debug("get_intra_role_latency: %s %s" % (role, self._latency))
        if 'IntraRoleLatency' not in self._latency:
            return None

        if role in self._latency['IntraRoleLatency']:
            return self._latency['IntraRoleLatency'][role]
        return None 

    def get_inter_role_latency(self, role1, role2):
        if 'InterRoleLatency' not in self._latency:
            return None

        set_a = "%s_%s" % (role1, role2)
        set_b = "%s_%s" % (role2, role1)

        connection = []
        for i in self._latency['InterRoleLatency']:
            if i == set_a or i == set_b:
                connection.append(self._latency['InterRoleLatency'][i])
        logger.debug("get_inter_role_latency: %s %s -> %s" % (role1, role2, connection))

        if len(connection) > 1:
            raise Exception("Multiple latencies for the same connection are not supported %s" % connection)
        if len(connection) == 0:
            return None
        return connection[0]

    # Returns a dictionary of the latency + associated mark code
    def get_all_latencies(self):
        mark = 10
        latencies = {}
        default = self.get_default_latency()
        if default != None:
            latencies[default] = mark
        mark = mark + 1
        if 'IntraRoleLatency' in self._latency:
            for i in self._latency['IntraRoleLatency']:
                l = self._latency['IntraRoleLatency'][i]
                latencies[l] = mark
                mark = mark + 1
        if 'InterRoleLatency' in self._latency:
            for i in self._latency['InterRoleLatency']:
                l = self._latency['InterRoleLatency'][i]
                latencies[l] = mark
                mark = mark + 1
        return latencies

    def get_mark(self, latency):
        latencies = self.get_all_latencies()
        logger.debug("get_mark: %s -> %s" % (latency, latencies))
        return latencies[latency]

    # returns (latency,mark,remotehost) given a host and a destination ip
    # The algorithm works like this:
    # 1) if the ip belongs to the host return None
    # 2) if the ip belongs to the intrarole map, return that latency
    # 3) if the ip belongs to the interrole map, return that latency
    # 4) return default latency
    def get_latency(self, host, ip):
        # Does the ip belong to ourselves -> no latency
        if ip in self.get_host_ips(host):
            return None
        all_latencies = self.get_all_latencies()
        all_hosts = set(self.get_hosts())
        host_role = self.get_host_role(host)
        remote_host = self.get_ip_host(ip)
        remote_role = self.get_host_role(remote_host)
        latency = self.get_default_latency()
        if host_role == remote_role:
            l = self.get_intra_role_latency(host_role)
            if l != None: # If no intrarole latency is specified return none
                latency = l

        inter_latency = self.get_inter_role_latency(host_role, remote_role)
        if inter_latency != None:
            latency = inter_latency

        logger.debug("get_latency %s %s %s %s" % (remote_host, remote_role, latency, inter_latency))
        if latency != None:
            return (latency, self.get_mark(latency), remote_host)
        return None


def generate_latencies(inventory):
    # For each host find the in the role, we find the ips of all the *other*
    # hosts in the role and we add the delay for those specific IPs
    hosts = set(inventory.get_hosts())
    for host in hosts:
        if host.startswith('undercloud'):
            continue
        s = set()
        s.add(host)
        other_hosts = hosts - s
        ips = []
        for remote in other_hosts:
            tmp = inventory.get_host_ips(remote)
            logger.debug("Latencies for %s. Adding %s IPs from %s" % (host, tmp, remote))
            ips.extend(tmp)
        vips = inventory.get_all_vips()
        ips.extend(vips)
        logger.debug("Added VIPs %s" % vips)
        logger.debug("Total of remote IPs for %s: %s -> %s" % (host, len(ips), ips))
        latencies = {}
        for i in ips:
            lat = inventory.get_latency(host, i)
            logger.debug("Latency for %s (%s) -> %s (%s, %s): %s" % (host, inventory.get_host_role(host), 
                inventory.get_ip_host(i), inventory.get_ip_role(i), i, lat))
            if lat != None:
                latencies[i] = lat


        logger.debug(inventory.get_all_latencies())
        all_ips = ips[:]
        all_ips.extend(inventory.get_host_ips(host))

        j2_env = Environment(loader=FileSystemLoader(BASEDIR), trim_blocks=True)
        d = {}
        d['host'] = host
        d['all_ips'] = all_ips
        d['other_ips'] = " ".join(ips)
        d['other_hosts'] = other_hosts
        d['latencies'] = latencies
        d['all_latencies'] = inventory.get_all_latencies()
        f = open(os.path.join(OUTPUTDIR, "%s-tc.sh" % host), "w")
        f.write(j2_env.get_template('tc-intra.sh.j2').render(d))
        f.close()
        os.chmod(os.path.join(OUTPUTDIR, "%s-tc.sh" % host), 0755)

def generate_distribution_script(inventory):
    hosts = inventory.get_hosts()
    j2_env = Environment(loader=FileSystemLoader(BASEDIR), trim_blocks=True)
    t = {}
    for i in hosts:
         if i.startswith('undercloud'):
             continue
         t[i] = inventory.get_host_ip_network(i, 'ctlplane')
    d = {}
    d['hosts'] = t
    d['outputdir' ] = OUTPUTDIR
    f = open(os.path.join(OUTPUTDIR, "distribute-tc-scripts.sh"), "w")
    f.write(j2_env.get_template('distribute-tc-scripts.sh.j2').render(d))
    f.close()
    os.chmod(os.path.join(OUTPUTDIR, "distribute-tc-scripts.sh"), 0755)

def generate(invfile, delayfile):
    inv = Inventory(invfile, delayfile)
    generate_latencies(inv)
    generate_distribution_script(inv)

def main(args):
    if len(args) != 2:
        print("Pass an inventory file as a parameter and a yaml file with latency description. For example:")
        print("%s example-cloud.yaml example-latencies.yaml" % args[0])
        help = """
DefaultLatency: 5ms
IntraRoleLatency:
  Controller: 20ms
  Compute: 10ms
InterRoleLatency:
  Controller_Compute: 100ms
  Compute_CephStorage: 100ms
  Controller_CephStorage: 100ms
"""
        print(help)

        sys.exit(1)

    generate(args[0], args[1])
    print("The generated scripts are in %s" % OUTPUTDIR)
    print("Run \"%s/%s\" to distribute them on all nodes" % (OUTPUTDIR, "distribute-tc-scripts.sh"))

if __name__ == '__main__':
    main(sys.argv[1:])
