import os.path as P
import subprocess as S
from string import Template
from typing import Dict, List, Optional

import pkg_resources

from hare_mp.store import ValueProvider
from hare_mp.types import (DList, Maybe, DiskRef, PoolDesc, ProfileDesc,
                           NodeDesc, ClusterDesc, Protocol, Text)

DHALL_PATH = '/opt/seagate/cortx/hare/share/cfgen/dhall'
DHALL_EXE = '/opt/seagate/cortx/hare/bin/dhall'
DHALL_TO_YAML_EXE = '/opt/seagate/cortx/hare/bin/dhall-to-yaml'


class CdfGenerator:
    def __init__(self, provider: ValueProvider):
        super().__init__()
        self.provider = provider

    def _get_dhall_path(self) -> str:
        if P.exists(DHALL_PATH):
            return DHALL_PATH
        raise RuntimeError('CFGEN Dhall types not found')

    def _gencdf(self) -> str:
        resource_path = '/'.join(('dhall', 'gencdf.dhall'))
        raw_content: bytes = pkg_resources.resource_string(
            'hare_mp', resource_path)
        return raw_content.decode('utf-8')

    def _get_cluster_id(self) -> str:
        conf = self.provider

        # We will read 'cluster_id' of 1st 'machine_id' present in server_node
        server_node = conf.get('server_node')
        machine_id = list(server_node.keys())[0]
        cluster_id = server_node[machine_id]['cluster_id']
        return cluster_id

    def _create_node_descriptions(self) -> List[NodeDesc]:
        nodes: List[NodeDesc] = []
        conf = self.provider
        server_dict: Dict[str, str] = conf.get('cluster>server_nodes')
        for _, node in server_dict.items():
            nodes.append(self._create_node(node))
        return nodes

    def _create_pool_descriptions(self) -> List[PoolDesc]:
        pools: List[PoolDesc] = []
        conf = self.provider
        cluster_id = self._get_cluster_id()
        storage_set_count = int(
            conf.get(f'cluster>{cluster_id}>site>storage_set_count'))

        for x in range(storage_set_count):
            storage_set_name = conf.get(
                f'cluster>{cluster_id}>storage_set{x+1}>name')

            data_devices_count: int = 0
            for node in conf.get(
                    f'cluster>{cluster_id}>storage_set{x+1}>server_nodes'):
                data_devices_count += len(
                    conf.get(f'cluster>{node}>storage>data_devices'))

            data_units_count = int(conf.get(
                f'cluster>{cluster_id}>storage_set{x+1}>durability>data'))
            parity_units_count = int(conf.get(
                f'cluster>{cluster_id}>storage_set{x+1}>durability>parity'))
            spare_units_count = int(conf.get(
                f'cluster>{cluster_id}>storage_set{x+1}>durability>spare'))

            if (data_devices_count != 0
                and not data_devices_count >=
                    data_units_count + parity_units_count + spare_units_count):
                raise RuntimeError('Invalid storage set configuration')

            pools.append(PoolDesc(
                name=Text(storage_set_name),
                disk_refs=Maybe(DList([
                    DiskRef(path=Text(device), node=Maybe(Text(node), 'Text'))
                    for node in conf.get(
                        f'cluster>{cluster_id}>storage_set{x+1}>server_nodes')
                    for device in conf.get(
                        f'cluster>{node}>storage>data_devices')
                ], 'List DiskRef'), 'List DiskRef'),
                data_units=data_units_count,
                parity_units=parity_units_count))

        return pools

    def _create_profile_descriptions(
        self, pool_desc: List[PoolDesc]
    ) -> List[ProfileDesc]:
        profiles: List[ProfileDesc] = []

        profiles.append(ProfileDesc(
            name=Text('Profile_the_pool'),
            pools=DList([pool.name
                         for pool in pool_desc
                         ], 'List Text')))

        return profiles

    def _get_cdf_dhall(self) -> str:
        dhall_path = self._get_dhall_path()
        nodes = self._create_node_descriptions()
        pools = self._create_pool_descriptions()
        profiles = self._create_profile_descriptions(pools)

        params_text = str(ClusterDesc(
            node_info=nodes, pool_info=pools, profile_info=profiles))
        gencdf = Template(self._gencdf()).substitute(path=dhall_path,
                                                     params=params_text)
        return gencdf

    def generate(self) -> str:
        gencdf = self._get_cdf_dhall()

        dhall = S.Popen([DHALL_EXE],
                        stdin=S.PIPE,
                        stdout=S.PIPE,
                        stderr=S.PIPE,
                        encoding='utf8')

        dhall_out, err_d = dhall.communicate(input=gencdf)
        if dhall.returncode:
            raise RuntimeError(f'dhall binary failed: {err_d}')

        to_yaml = S.Popen([DHALL_TO_YAML_EXE],
                          stdin=S.PIPE,
                          stdout=S.PIPE,
                          stderr=S.PIPE,
                          encoding='utf8')

        yaml_out, err = to_yaml.communicate(input=dhall_out)
        if to_yaml.returncode:
            raise RuntimeError(f'dhall-to-yaml binary failed: {err}')
        return yaml_out

    def _get_iface(self, nodename: str) -> str:
        ifaces = self.provider.get(
            f'cluster>{nodename}>network>data>private_interfaces')
        if not ifaces:
            raise RuntimeError('No data network interfaces found')
        return ifaces[0]

    def _get_iface_type(self, nodename: str) -> Optional[Protocol]:
        iface = self.provider.get(
            f'cluster>{nodename}>network>data>interface_type', allow_null=True)
        if iface is None:
            return None
        return Protocol[iface]

    def _create_node(self, name: str) -> NodeDesc:
        store = self.provider
        hostname = store.get(f'cluster>{name}>hostname')
        iface = self._get_iface(name)
        return NodeDesc(
            hostname=Text(hostname),
            data_iface=Text(iface),
            data_iface_type=Maybe(self._get_iface_type(name), 'P'),
            io_disks=DList([
                Text(device)
                for device in store.get(f'cluster>{name}>storage>data_devices')
            ], 'List Text'),
            #
            # [KN] This is a hotfix for singlenode deployment
            # TODO in the future the value must be taken from a correct
            # ConfStore key (it doesn't exist now).
            meta_data=Text('/dev/vg_metadata_srvnode-1/lv_raw_metadata'),
            s3_instances=int(store.get(f'cluster>{name}>s3_instances')))