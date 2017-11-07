#!/usr/bin/env python3
"""
backend.py - ffmap-backend runner
https://github.com/ffnord/ffmap-backend
"""
import argparse
import json
import os
import sys
from datetime import datetime

import networkx as nx
from networkx.readwrite import json_graph

from lib import graph, nodes
from lib.alfred import Alfred
from lib.batman import Batman
from lib.rrddb import RRD
from lib.nodelist import export_nodelist
from lib.validate import validate_nodeinfos

NODES_VERSION = 1
GRAPH_VERSION = 1


def main(params):
    os.makedirs(params['dest_dir'], exist_ok=True)

    nodes_fn = os.path.join(params['dest_dir'], 'nodes.json')
    graph_fn = os.path.join(params['dest_dir'], 'graph.json')
    nodelist_fn = os.path.join(params['dest_dir'], 'nodelist.json')

    now = datetime.utcnow().replace(microsecond=0)

    # parse mesh param and instantiate Alfred/Batman instances
    alfred_instances = []
    batman_instances = []
    for value in params['mesh']:
        # (1) only batman-adv if, no alfred sock
        if ':' not in value:
            if len(params['mesh']) > 1:
                raise ValueError(
                    'Multiple mesh interfaces require the use of '
                    'alfred socket paths.')
            alfred_instances.append(Alfred(unix_sockpath=None))
            batman_instances.append(Batman(mesh_interface=value))
        else:
            # (2) batman-adv if + alfred socket
            try:
                batif, alfredsock = value.split(':')
                alfred_instances.append(Alfred(unix_sockpath=alfredsock))
                batman_instances.append(Batman(mesh_interface=batif,
                                               alfred_sockpath=alfredsock))
            except ValueError:
                raise ValueError(
                    'Unparseable value "{0}" in --mesh parameter.'.
                    format(value))

    # read nodedb state from node.json
    try:
        with open(nodes_fn, 'r') as nodedb_handle:
            nodedb = json.load(nodedb_handle)
    except (IOError, ValueError):
        nodedb = {'nodes': dict()}

    # flush nodedb if it uses the old format
    if 'links' in nodedb:
        nodedb = {'nodes': dict()}

    # set version we're going to output
    nodedb['version'] = NODES_VERSION

    # update timestamp and assume all nodes are offline
    nodedb['timestamp'] = now.isoformat()
    for node_id, node in nodedb['nodes'].items():
        node['flags']['online'] = False

    # integrate alfred nodeinfo
    for alfred in alfred_instances:
        nodeinfo = validate_nodeinfos(alfred.nodeinfo())
        nodes.import_nodeinfo(nodedb['nodes'], nodeinfo,
                              now, assume_online=True)

    # integrate static aliases data
    for aliases in params['aliases']:
        with open(aliases, 'r') as f:
            nodeinfo = validate_nodeinfos(json.load(f))
            nodes.import_nodeinfo(nodedb['nodes'], nodeinfo,
                                  now, assume_online=False)

    nodes.reset_statistics(nodedb['nodes'])
    for alfred in alfred_instances:
        nodes.import_statistics(nodedb['nodes'], alfred.statistics())

    # acquire gwl and visdata for each batman instance
    mesh_info = []
    for batman in batman_instances:
        vd = batman.vis_data()
        gwl = batman.gateway_list()

        mesh_info.append((vd, gwl))

    # integrate static online aliases data

    gwc = {}

    # parse aliases for gateways
    for aliases in params['aliases_online']:
        with open(aliases, 'r') as f:
            nodeinfo = validate_nodeinfos(json.load(f))
            for node in nodeinfo:
                try:
                    system = nodedb['nodes'][node.get('node_id')]['nodeinfo'].get('system', None)
                    if system:
                        # if role is gatway add mac to gateway list (gwl)
                        if nodedb['nodes'][node.get('node_id')]['nodeinfo']['system'].get('role') == 'gateway':
                            mac = nodedb['nodes'][node.get('node_id')]['nodeinfo']['network'].get('mac', None)
                            if mac:
                                # extend gwl
                                gwl.append(mac)
                                # fake visdata
                                vd.append({"primary": mac})
                                gwc[mac] = 0
                except KeyError:
                    pass

    # parse data from aliases
    for aliases in params['aliases_online']:
        with open(aliases, 'r') as f:
            nodeinfo = validate_nodeinfos(json.load(f))
            nodes.import_nodeinfo(nodedb['nodes'], nodeinfo,
                                  now, assume_online=True)
            for node in nodeinfo:
                gateways = nodedb['nodes'][node.get('node_id')]['nodeinfo']['network'].get('gateway', None)
                # if gateway in gwl fake visdata
                if gateways:
                    if isinstance(gateways, str):
                        gateways = [ gateways ]
                    for gateway in gateways:
                        gateway = ':'.join(gateway[i:i+2] for i in range(0,12,2))
                        if gateway in gwl:
                            mac = nodedb['nodes'][node.get('node_id')]['nodeinfo']['network'].get('mac', None)
                            if mac:
                                # fake visdata
                                vd.append({"primary": mac})
                                vd.append({"router": mac, "label": "TT", "gateway": gateway})
                                vd.append({"router": mac, "label": "1.000", "neighbor": gateway})
                                # count node to gateway clients
                                try:
                                    gwc[gateway] += 1
                                except KeyError:
                                    pass
                # add Statistics from aliases
                try:
                    statistics = node.get('statistics', None)
                except KeyError:
                    pass
                if statistics:
                    if nodedb['nodes'][node.get('node_id')].get('statistics'):
                        nodedb['nodes'][node.get('node_id')]['statistics'].update(statistics)
                        nodedb['nodes'][node.get('node_id')]['nodeinfo'].pop('statistics')

    if params['aliases_online']:
        # update gateway clients
        for gw in gwc:
            node = gw.replace(':', '')
            try:
                clients = nodedb['nodes'][node]['statistics'].get('clients')
                nodedb['nodes'][node]['statistics']['clients'] = clients + gwc[gw]
                clients = nodedb['nodes'][node]['statistics'].get('clients')
            except KeyError:
                pass
        # rebuild mesh_info
        mesh_info = []
        mesh_info.append((vd, gwl))

    # update nodedb from batman-adv data
    for vd, gwl in mesh_info:
        nodes.import_mesh_ifs_vis_data(nodedb['nodes'], vd)
        nodes.import_vis_clientcount(nodedb['nodes'], vd)
        nodes.mark_vis_data_online(nodedb['nodes'], vd, now)
        nodes.mark_gateways(nodedb['nodes'], gwl)

    # clear the nodedb from nodes that have not been online in $prune days
    if params['prune']:
        nodes.prune_nodes(nodedb['nodes'], now, params['prune'])

    # build nxnetworks graph from nodedb and visdata
    batadv_graph = nx.DiGraph()
    for vd, gwl in mesh_info:
        graph.import_vis_data(batadv_graph, nodedb['nodes'], vd)

    # force mac addresses to be vpn-link only (like gateways for example)
    if params['vpn']:
        graph.mark_vpn(batadv_graph, frozenset(params['vpn']))

    def extract_tunnel(nodes):
        macs = set()
        for id, node in nodes.items():
            try:
                for mac in node["nodeinfo"]["network"]["mesh"]["bat0"]["interfaces"]["tunnel"]:
                    macs.add(mac)
            except KeyError:
                pass

        return macs

    graph.mark_vpn(batadv_graph, extract_tunnel(nodedb['nodes']))

    batadv_graph = graph.merge_nodes(batadv_graph)
    batadv_graph = graph.to_undirected(batadv_graph)

    # optional anonymize data (trigger with --anonymize)
    if params['anonymize']:
        for node in nodedb['nodes']:
            if nodedb['nodes'][node]['nodeinfo'].get('owner'):
                nodedb['nodes'][node]['nodeinfo'].pop('owner')

    # write processed data to dest dir
    with open(nodes_fn, 'w') as f:
        json.dump(nodedb, f)

    graph_out = {'batadv': json_graph.node_link_data(batadv_graph),
                 'version': GRAPH_VERSION}

    with open(graph_fn, 'w') as f:
        json.dump(graph_out, f)

    with open(nodelist_fn, 'w') as f:
        json.dump(export_nodelist(now, nodedb), f)

    # optional rrd graphs (trigger with --rrd)
    if params['rrd']:
        script_directory = os.path.dirname(os.path.realpath(__file__))
        rrd = RRD(os.path.join(script_directory, 'nodedb'),
                  os.path.join(params['dest_dir'], 'nodes'),
                  params['display_time_global'],
                  params['display_time_node'])
        rrd.update_database(nodedb['nodes'])
        rrd.update_images()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()

    parser.add_argument('-a', '--aliases',
                        help='Read aliases from FILE',
                        nargs='+', default=[], metavar='FILE')
    parser.add_argument('-m', '--mesh',
                        default=['bat0'], nargs='+',
                        help='Use given batman-adv mesh interface(s) (defaults'
                             'to bat0); specify alfred unix socket like '
                             'bat0:/run/alfred0.sock.')
    parser.add_argument('-d', '--dest-dir', action='store',
                        help='Write output to destination directory',
                        required=True)
    parser.add_argument('-V', '--vpn', nargs='+', metavar='MAC',
                        help='Assume MAC addresses are part of vpn')
    parser.add_argument('-p', '--prune', metavar='DAYS', type=int,
                        help='forget nodes offline for at least DAYS')
    parser.add_argument('--with-rrd', dest='rrd', action='store_true',
                        default=False,
                        help='enable the rendering of RRD graphs (cpu '
                             'intensive)')
    parser.add_argument('--rrd-time-global', dest='display_time_global', action='store',
                        default='7d', metavar='TIME',
                        help='set the time range for the global RRD graph')
    parser.add_argument('--rrd-time-node', dest='display_time_node', action='store',
                        default='1d', metavar='TIME',
                        help='set the time range for the node RRD graph')
    parser.add_argument('--anonymize', dest='anonymize', action='store_true',
                        default=False,
                        help='remove contact information')
    parser.add_argument('-o', '--online-aliases', dest='aliases_online',
                        help='Read online aliases from FILE',
                        nargs='+', default=[], metavar='FILE')

    options = vars(parser.parse_args())
    main(options)
