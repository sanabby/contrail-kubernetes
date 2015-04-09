import logging
import re
import subprocess
import sys
from shell import Shell

class LxcManager(object):
    def __init__(self):
        pass

    def _interface_generate_unique_name(self):
        output = Shell.run('ip link list')
        ids = {}

        for line in output.split('\n'):
            m = re.match(r'[\d]+: instance([\d]+)', line)
            if m:
                ids[m.group(1)] = True

        for i in range(256):
            if str(i) in ids:
                continue
            return 'instance%d' % i
        return None

    # Find the peer interface (in the host) for a given nsname
    # The two ends of the interface link peerings have adjacent ifindex
    def interface_find_peer_name(self, ifname_instance, nsname):
        # Get ifindex of ifname_instance
        ns_ifindex = Shell.run('ip netns exec %s ethtool -S %s | '
                               'grep peer_ifindex | awk "{print $2}"' \
                               % (nsname, ifname_instance))

        # Get the list of docker0 bridge member interface names.
        bridge_members = [ member_if[member_if.find("veth"):] for member_if in \
            Shell.run("brctl show docker0 | grep veth").split("\n") \
        ]

        # Remove the trailing empty string, which comes as a result of split.
        bridge_members.pop()

        # Get all member interfaces' ifindex
        bridge_members_ifindex = [ Shell.run( \
            "ethtool -S %s | grep peer_ifindex | awk '{print $2}'" % i) \
                for i in bridge_members ]

        # Peer interface ifindex is one less than that of container intf's index
        try:
            member_index = bridge_members_ifindex.index('%s\n' % \
                (int(ns_ifindex) - 1))
        except:
            logging.info('did not find member %s' \
                         % bridge_members[member_index])
            logging.error('Cannot find peer interface name')
            raise
        logging.info('Peer interface found: %s' % bridge_members[member_index])
        return bridge_members[member_index]

    # Move the interface out of the docker0 bridge and attach it to contrail
    # Return the moved interface name
    def move_interface(self, nsname, pid, ifname_instance, vmi):
        ifname_master = self.interface_find_peer_name(ifname_instance, nsname)

        # Remove the interface from the bridge
        Shell.run('brctl delif docker0 %s' % ifname_master)
        if vmi:
            # Set interface mac and remove any IP address already assigned
            mac = vmi.virtual_machine_interface_mac_addresses.mac_address[0]
            Shell.run('ip netns exec %s ifconfig eth0 hw ether %s' %
                      (nsname, mac))
            Shell.run('ip netns exec %s ip addr flush dev %s' %
                      (nsname, ifname_instance))
        return ifname_master

    def create_interface(self, nsname, ifname_instance, vmi=None):
        ifname_master = self._interface_generate_unique_name()
        Shell.run('ip link add %s type veth peer name %s' %
                  (ifname_instance, ifname_master))
        if vmi:
            mac = vmi.virtual_machine_interface_mac_addresses.mac_address[0]
            Shell.run('ifconfig %s hw ether %s' % (ifname_instance, mac))

        Shell.run('ip link set %s netns %s' % (ifname_instance, nsname))
        Shell.run('ip link set %s up' % ifname_master)
        return ifname_master

    def _interface_list_contains(self, output, iface):
        for line in output.split('\n'):
            m = re.match(r'[\d]+: ' + iface + ':', line)
            if m:
                return True
        return False

    def _get_master_ifname(self, daemon, ifname_instance):
        output = Shell.run('ip netns exec ns-%s ethtool -S %s' %
                           (daemon, ifname_instance))
        m = re.search(r'peer_ifindex: (\d+)', output)
        ifindex = m.group(1)
        output = Shell.run('ip link list')
        expr = '^' + ifindex + ': (\w+): '
        regex = re.compile(expr, re.MULTILINE)
        m = regex.search(output)
        return m.group(1)

    def interface_update(self, daemon, vmi, ifname_instance):
        """
        1. Make sure that the interface exists in the name space.
        2. Update the mac address.
        """
        output = Shell.run('ip netns exec ns-%s ip link list' % daemon)
        if not self._interface_list_contains(output, ifname_instance):
            ifname_master = self.create_interface('ns-%s' % daemon, ifname_instance)
        else:
            ifname_master = self._get_master_ifname(daemon, ifname_instance)

        mac = vmi.virtual_machine_interface_mac_addresses.mac_address[0]
        Shell.run('ip netns exec ns-%s ifconfig %s hw ether %s' %
                  (daemon, ifname_instance, mac))
        return ifname_master

    def interface_config(self, daemon, ifname_guest, advertise_default=True,
                         ip_prefix=None):
        """
        Once the interface is operational, configure the IP addresses.
        For a bi-directional interface we use dhclient.
        """
        if advertise_default:
            Shell.run('ip netns exec ns-%s dhclient %s' %
                      (daemon, ifname_guest))
        else:
            Shell.run('ip netns exec ns-%s ip addr add %s/%d dev %s' %
                      (daemon, ip_prefix[0], ip_prefix[1], ifname_guest))
            Shell.run('ip netns exec ns-%s ip link set %s up' %
                      (daemon, ifname_guest))
            # disable reverse path filtering
            Shell.run('ip netns exec ns-%s sh -c ' +
                      '"echo 2 >/proc/sys/net/ipv4/conf/%s/rp_filter"' %
                      (daemon, ifname_guest))

    def clear_interfaces(self, nsname):
        Shell.run('ip netns exec %s dhclient -r' % nsname)
        output = Shell.run('ip netns exec %s ip link list' % nsname)
        for line in output.split('\n'):
            m = re.match(r'^[\d]+: ([\w]+):', line)
            if m:
                ifname = m.group(1)
                if ifname == 'lo':
                    continue
                Shell.run('ip netns exec %s ip link delete %s' %
                          (nsname, ifname))

    def namespace_init(self, daemon):
        output = Shell.run('ip netns list')
        for line in output.split():
            if line == 'ns-' + daemon:
                return False
        Shell.run('ip netns add ns-%s' % daemon)
        return True

    def namespace_delete(self, daemon):
        Shell.run('ip netns delete ns-%s' % daemon)
