#
#  Copyright (c) 2021 International Business Machines
#  All rights reserved.
#
#  SPDX-License-Identifier: LGPL-3.0-or-later
#
#  Authors: anita.shekar@ibm.com, sandy.kaur@ibm.com
#

import socket
import grpc
import json
import uuid
import random
import logging
import os
import threading
import errno
import contextlib

import spdk.rpc.bdev as rpc_bdev
import spdk.rpc.nvmf as rpc_nvmf
import spdk.rpc.log as rpc_log
from spdk.rpc.client import JSONRPCException

from google.protobuf import json_format
from .proto import gateway_pb2 as pb2
from .proto import gateway_pb2_grpc as pb2_grpc
from .config import GatewayConfig
from .config import GatewayEnumUtils
from .config import GatewayLogger
from .state import GatewayState

MAX_ANA_GROUPS = 4

class GatewayService(pb2_grpc.GatewayServicer):
    """Implements gateway service interface.

    Handles configuration of the SPDK NVMEoF target according to client requests.

    Instance attributes:
        config: Basic gateway parameters
        logger: Logger instance to track server events
        gateway_name: Gateway identifier
        gateway_state: Methods for target state persistence
        spdk_rpc_client: Client of SPDK RPC server
    """

    def __init__(self, config, gateway_state, omap_lock, spdk_rpc_client) -> None:
        """Constructor"""
        self.logger = GatewayLogger(config).logger
        ver = os.getenv("NVMEOF_VERSION")
        if ver:
            self.logger.info(f"Using NVMeoF gateway version {ver}")
        spdk_ver = os.getenv("NVMEOF_SPDK_VERSION")
        if spdk_ver:
            self.logger.info(f"Using SPDK version {spdk_ver}")
        ceph_ver = os.getenv("NVMEOF_CEPH_VERSION")
        if ceph_ver:
            self.logger.info(f"Using vstart cluster version based on {ceph_ver}")
        build_date = os.getenv("BUILD_DATE")
        if build_date:
            self.logger.info(f"NVMeoF gateway built on: {build_date}")
        git_rep = os.getenv("NVMEOF_GIT_REPO")
        if git_rep:
            self.logger.info(f"NVMeoF gateway Git repository: {git_rep}")
        git_branch = os.getenv("NVMEOF_GIT_BRANCH")
        if git_branch:
            self.logger.info(f"NVMeoF gateway Git branch: {git_branch}")
        git_commit = os.getenv("NVMEOF_GIT_COMMIT")
        if git_commit:
            self.logger.info(f"NVMeoF gateway Git commit: {git_commit}")
        git_modified = os.getenv("NVMEOF_GIT_MODIFIED_FILES")
        if git_modified:
            self.logger.info(f"NVMeoF gateway uncommitted modified files: {git_modified}")
        self.config = config
        config.dump_config_file(self.logger)
        self.rpc_lock = threading.Lock()
        self.gateway_state = gateway_state
        self.omap_lock = omap_lock
        self.spdk_rpc_client = spdk_rpc_client
        self.gateway_name = self.config.get("gateway", "name")
        if not self.gateway_name:
            self.gateway_name = socket.gethostname()
        self.gateway_group = self.config.get("gateway", "group")
        self._init_cluster_context()

    def parse_json_exeption(self, ex):
        if type(ex) != JSONRPCException:
            return None

        json_error_text = "Got JSON-RPC error response"
        rsp = None
        try:
            resp_index = ex.message.find(json_error_text)
            if resp_index >= 0:
                resp_str = ex.message[resp_index + len(json_error_text) :]
                resp_index = resp_str.find("response:")
                if resp_index >= 0:
                    resp_str = resp_str[resp_index + len("response:") :]
                    resp = json.loads(resp_str)
        except Exception as jsex:
            self.logger.error(f"Got exception parsing JSon exception: {jsex}")
            pass
        if resp:
            if resp["code"] < 0:
                resp["code"] = -resp["code"]
        return resp

    def _init_cluster_context(self) -> None:
        """Init cluster context management variables"""
        self.clusters = {}
        self.current_cluster = None
        self.bdevs_per_cluster = self.config.getint_with_default("spdk", "bdevs_per_cluster", 8)
        if self.bdevs_per_cluster < 1:
            raise Exception(f"invalid configuration: spdk.bdevs_per_cluster_contexts {self.bdevs_per_cluster} < 1")
        self.librbd_core_mask = self.config.get_with_default("spdk", "librbd_core_mask", None)
        self.rados_id = self.config.get_with_default("ceph", "id", "")
        if self.rados_id == "":
            self.rados_id = None

    def _get_cluster(self) -> str:
        """Returns cluster name, enforcing bdev per cluster context"""
        cluster_name = None
        if self.current_cluster is None:
            cluster_name = self._alloc_cluster()
            self.current_cluster = cluster_name
            self.clusters[cluster_name] = 1
        elif self.clusters[self.current_cluster] >= self.bdevs_per_cluster:
            self.current_cluster = None
            cluster_name = self._get_cluster()
        else:
            cluster_name = self.current_cluster
            self.clusters[cluster_name] += 1

        return cluster_name

    def _alloc_cluster(self) -> str:
        """Allocates a new Rados cluster context"""
        name = f"cluster_context_{len(self.clusters)}"
        self.logger.info(f"Allocating cluster {name=}")
        rpc_bdev.bdev_rbd_register_cluster(
            self.spdk_rpc_client,
            name = name,
            user = self.rados_id,
            core_mask = self.librbd_core_mask,
        )
        return name

    def _grpc_function_with_lock(self, func, request, context):
        with self.rpc_lock:
            return func(request, context)

    def execute_grpc_function(self, func, request, context):
        """This functions handles both the RPC and OMAP locks. It first takes the OMAP lock and then calls a
           help function which takes the RPC lock and call the GRPC function passes as a parameter. So, the GRPC
           function runs with both the OMAP and RPC locks taken
        """
        return self.omap_lock.execute_omap_locking_function(self._grpc_function_with_lock, func, request, context)

    def create_bdev(self, name, uuid, rbd_pool_name, rbd_image_name, block_size):
        """Creates a bdev from an RBD image."""

        self.logger.info(f"Received request to create bdev {name} from"
                         f" {rbd_pool_name}/{rbd_image_name}"
                         f" with block size {block_size}")
        try:
            bdev_name = rpc_bdev.bdev_rbd_create(
                self.spdk_rpc_client,
                name=name,
                cluster_name=self._get_cluster(),
                pool_name=rbd_pool_name,
                rbd_name=rbd_image_name,
                block_size=block_size,
                uuid=uuid,
            )
            self.logger.info(f"create_bdev: {bdev_name}")
        except Exception as ex:
            errmsg = f"create_bdev {name} failed with:\n{ex}"
            self.logger.error(errmsg)
            resp = self.parse_json_exeption(ex)
            status = errno.EINVAL
            if resp:
                status = resp["code"]
                errmsg = f"Failure creating bdev {name}: {resp['message']}"
            return pb2.bdev_status(status=status, error_message=errmsg)

        # Just in case SPDK failed with no exception
        if not bdev_name:
            errmsg = f"Can't create bdev {name}"
            self.logger.error(errmsg)
            return pb2.bdev_status(status=errno.EINVAL, error_message=errmsg)

        if name != bdev_name:
            self.logger.warning(f"Created bdev name {bdev_name} differs from requested name {name}")

        return pb2.bdev_status(bdev_name=name, status=0, error_message=os.strerror(0))

    def resize_bdev(self, bdev_name, new_size):
        """Resizes a bdev."""

        self.logger.info(f"Received request to resize bdev {bdev_name} to {new_size} MiB")
        with self.rpc_lock:
            try:
                ret = rpc_bdev.bdev_rbd_resize(
                    self.spdk_rpc_client,
                    name=bdev_name,
                    new_size=new_size,
                )
                self.logger.info(f"resize_bdev {bdev_name}: {ret}")
            except Exception as ex:
                errmsg = f"Failure resizing bdev {bdev_name}:\n{ex}"
                self.logger.error(errmsg)
                resp = self.parse_json_exeption(ex)
                status = errno.EINVAL
                if resp:
                    status = resp["code"]
                    errmsg = f"Failure resizing bdev {bdev_name}: {resp['message']}"
                return pb2.req_status(status=status, error_message=errmsg)

            if not ret:
                errmsg = f"Failure resizing bdev {bdev_name}"
                self.logger.error(errmsg)
                return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

            return pb2.req_status(status=0, error_message=os.strerror(0))

    def delete_bdev(self, bdev_name):
        """Deletes a bdev."""

        self.logger.info(f"Received request to delete bdev {bdev_name}")
        try:
            ret = rpc_bdev.bdev_rbd_delete(
                self.spdk_rpc_client,
                bdev_name,
            )
            self.logger.info(f"delete_bdev {bdev_name}: {ret}")
        except Exception as ex:
            errmsg = f"Failure deleting bdev {bdev_name}:\n{ex}"
            self.logger.error(errmsg)
            resp = self.parse_json_exeption(ex)
            status = errno.EINVAL
            if resp:
                status = resp["code"]
                errmsg = f"Failure deleting bdev {bdev_name}: {resp['message']}"
            return pb2.req_status(status=status, error_message=errmsg)

        # Just in case SPDK failed with no exception
        if not ret:
            errmsg = f"Failure deleting bdev {bdev_name}"
            self.logger.error(errmsg)
            return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

        return pb2.req_status(status=0, error_message=os.strerror(0))

    def is_discovery_nqn(self, nqn) -> bool:
        return nqn == GatewayConfig.DISCOVERY_NQN

    def subsystem_already_exists(self, context, nqn) -> bool:
        if not context:
            return False
        state = self.gateway_state.local.get_state()
        for key, val in state.items():
            if not key.startswith(self.gateway_state.local.SUBSYSTEM_PREFIX):
                continue
            try:
                subsys = json.loads(val)
                subnqn = subsys["subsystem_nqn"]
                if subnqn == nqn:
                    return True
            except Exception as ex:
                self.logger.warning(f"Got exception while parsing {val}:\n{ex}")
                continue
        return False

    def serial_number_already_used(self, context, serial) -> str:
        if not context:
            return None
        state = self.gateway_state.local.get_state()
        for key, val in state.items():
            if not key.startswith(self.gateway_state.local.SUBSYSTEM_PREFIX):
                continue
            try:
                subsys = json.loads(val)
                if serial == subsys["serial_number"]:
                    return subsys["subsystem_nqn"]
            except Exception as ex:
                self.logger.warning("Got exception while parsing {val}:\n{ex}")
                continue
        return None

    def create_subsystem_safe(self, request, context):
        """Creates a subsystem."""

        create_subsystem_error_prefix = f"Failure creating subsystem {request.subsystem_nqn}"

        self.logger.info(
            f"Received request to create subsystem {request.subsystem_nqn}, enable_ha: {request.enable_ha}, ana reporting: {request.ana_reporting}, context: {context}")

        errmsg = ""
        if self.is_discovery_nqn(request.subsystem_nqn):
            errmsg = f"{create_subsystem_error_prefix}: Can't create a discovery subsystem"
            ret = pb2.req_status(status = errno.EINVAL, error_message = errmsg)
            self.logger.error(f"{errmsg}")
            return ret
        if request.enable_ha and not request.ana_reporting:
            errmsg = f"{create_subsystem_error_prefix}: HA is enabled but ANA reporting is disabled"
            ret = pb2.req_status(status = errno.EINVAL, error_message = errmsg)
            self.logger.error(f"{errmsg}")
            return ret

        min_cntlid = self.config.getint_with_default("gateway", "min_controller_id", 1)
        max_cntlid = self.config.getint_with_default("gateway", "max_controller_id", 65519)
        if min_cntlid > max_cntlid:
            errmsg = f"{create_subsystem_error_prefix}: Min controller id {min_cntlid} is bigger than max controller id {max_cntlid}"
            ret = pb2.req_status(status = errno.EINVAL, error_message = errmsg)
            self.logger.error(f"{errmsg}")
            return ret

        if not request.serial_number:
            random.seed()
            randser = random.randint(2, 99999999999999)
            request.serial_number = f"SPDK{randser}"
            self.logger.info(f"No serial number specified for {request.subsystem_nqn}, will use {request.serial_number}")

        ret = False
        with self.omap_lock(context=context):
            errmsg = ""
            try:
                subsys_using_serial = None
                subsys_already_exists = self.subsystem_already_exists(context, request.subsystem_nqn)
                if subsys_already_exists:
                    errmsg = f"Subsystem already exists"
                else:
                    subsys_using_serial = self.serial_number_already_used(context, request.serial_number)
                    if subsys_using_serial:
                        errmsg = f"Serial number {request.serial_number} already used by subsystem {subsys_using_serial}"
                if subsys_already_exists or subsys_using_serial:
                    errmsg = f"{create_subsystem_error_prefix}: {errmsg}"
                    self.logger.error(f"{errmsg}")
                    return pb2.req_status(status=errno.EEXIST, error_message=errmsg)
                ret = rpc_nvmf.nvmf_create_subsystem(
                    self.spdk_rpc_client,
                    nqn=request.subsystem_nqn,
                    serial_number=request.serial_number,
                    max_namespaces=request.max_namespaces,
                    min_cntlid=min_cntlid,
                    max_cntlid=max_cntlid,
                    ana_reporting = request.ana_reporting,
                )
                self.logger.info(f"create_subsystem {request.subsystem_nqn}: {ret}")
            except Exception as ex:
                errmsg = f"{create_subsystem_error_prefix}:\n{ex}"
                self.logger.error(errmsg)
                resp = self.parse_json_exeption(ex)
                status = errno.EINVAL
                if resp:
                    status = resp["code"]
                    errmsg = f"{create_subsystem_error_prefix}: {resp['message']}"
                return pb2.req_status(status=status, error_message=errmsg)

            # Just in case SPDK failed with no exception
            if not ret:
                self.logger.error(create_subsystem_error_prefix)
                return pb2.req_status(status=errno.EINVAL, error_message=create_subsystem_error_prefix)

            if context:
                # Update gateway state
                try:
                    json_req = json_format.MessageToJson(
                        request, preserving_proto_field_name=True, including_default_value_fields=True)
                    self.gateway_state.add_subsystem(request.subsystem_nqn, json_req)
                except Exception as ex:
                    errmsg = f"Error persisting subsystem {request.subsystem_nqn}:\n{ex}"
                    self.logger.error(errmsg)
                    return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

        return pb2.req_status(status=0, error_message=os.strerror(0))

    def create_subsystem(self, request, context=None):
        return self.execute_grpc_function(self.create_subsystem_safe, request, context)

    def get_subsystem_namespaces(self, nqn) -> list:
        ns_list = []
        local_state_dict = self.gateway_state.local.get_state()
        for key, val in local_state_dict.items():
            if not key.startswith(self.gateway_state.local.NAMESPACE_PREFIX):
                continue
            try:
                ns = json.loads(val)
                if ns["subsystem_nqn"] == nqn:
                    nsid = ns["nsid"]
                    ns_list.append(nsid)
            except Exception as ex:
                self.logger.error(f"Got exception trying to get subsystem {nqn} namespaces:\n{ex}")
                pass

        return ns_list

    def subsystem_has_listeners(self, nqn) -> bool:
        local_state_dict = self.gateway_state.local.get_state()
        for key, val in local_state_dict.items():
            if not key.startswith(self.gateway_state.local.LISTENER_PREFIX):
                continue
            try:
                lsnr = json.loads(val)
                if lsnr["nqn"] == nqn:
                    return True
            except Exception as ex:
                self.logger.error(f"Got exception trying to get subsystem {nqn} listener:\n{ex}")
                pass

        return False

    def remove_subsystem_from_state(self, nqn, context):
        if not context:
            return pb2.req_status(status=0, error_message=os.strerror(0))

        # Update gateway state
        try:
            self.gateway_state.remove_subsystem(nqn)
        except Exception as ex:
            errmsg = f"Error persisting deletion of subsystem {nqn}:\n{ex}"
            self.logger.error(errmsg)
            return pb2.req_status(status=errno.EINVAL, error_message=errmsg)
        return pb2.req_status(status=0, error_message=os.strerror(0))

    def delete_subsystem_safe(self, request, context):
        """Deletes a subsystem."""

        delete_subsystem_error_prefix = f"Failure deleting subsystem {request.subsystem_nqn}"

        ret = False
        with self.omap_lock(context=context):
            try:
                ret = rpc_nvmf.nvmf_delete_subsystem(
                    self.spdk_rpc_client,
                    nqn=request.subsystem_nqn,
                )
                self.logger.info(f"delete_subsystem {request.subsystem_nqn}: {ret}")
            except Exception as ex:
                errmsg = f"{delete_subsystem_error_prefix}:\n{ex}"
                self.logger.error(errmsg)
                self.remove_subsystem_from_state(request.subsystem_nqn, context)
                resp = self.parse_json_exeption(ex)
                status = errno.EINVAL
                if resp:
                    status = resp["code"]
                    errmsg = f"{delete_subsystem_error_prefix}: {resp['message']}"
                return pb2.req_status(status=status, error_message=errmsg)

            # Just in case SPDK failed with no exception
            if not ret:
                self.logger.error(delete_subsystem_error_prefix)
                self.remove_subsystem_from_state( request.subsystem_nqn, context)
                return pb2.req_status(status=errno.EINVAL, error_message=delete_subsystem_error_prefix)

            return self.remove_subsystem_from_state(request.subsystem_nqn, context)

    def delete_subsystem(self, request, context=None):
        """Deletes a subsystem."""

        delete_subsystem_error_prefix = f"Failure deleting subsystem {request.subsystem_nqn}"
        self.logger.info(f"Received request to delete subsystem {request.subsystem_nqn}, context: {context}")

        if self.is_discovery_nqn(request.subsystem_nqn):
            errmsg = f"{delete_subsystem_error_prefix}: Can't delete a discovery subsystem"
            ret = pb2.req_status(status = errno.EINVAL, error_message = errmsg)
            self.logger.error(f"{errmsg}")
            return ret

        ns_list = []
        if context:
            if self.subsystem_has_listeners(request.subsystem_nqn):
                self.logger.warning(f"About to delete subsystem {request.subsystem_nqn} which has a listener defined")
            ns_list = self.get_subsystem_namespaces(request.subsystem_nqn)

        # We found a namespace still using this subsystem and --force wasn't used fail with EBUSY
        if not request.force and len(ns_list) > 0:
            errmsg = f"{delete_subsystem_error_prefix}: Namespace {ns_list[0]} is still using the subsystem. Either remove it or use the '--force' command line option"
            self.logger.error(errmsg)
            return pb2.req_status(status=errno.EBUSY, error_message=errmsg)

        for nsid in ns_list:
            # We found a namespace still using this subsystem and --force was used so we will try to remove the namespace
            self.logger.warning(f"Will remove namespace {nsid} from {request.subsystem_nqn}")
            ret = self.namespace_delete(pb2.namespace_delete_req(subsystem_nqn=request.subsystem_nqn, nsid=nsid), context)
            if ret.status == 0:
                self.logger.info(f"Automatically removed namespace {nsid} from {request.subsystem_nqn}")
            else:
                self.logger.error(f"Failure removing namespace {nsid} from {request.subsystem_nqn}:\n{ret.error_message}")
                self.logger.warning(f"Will continue deleting {request.subsystem_nqn} anyway")
        return self.execute_grpc_function(self.delete_subsystem_safe, request, context)

    def create_namespace(self, subsystem_nqn, bdev_name, nsid, anagrpid, uuid, context):
        """Adds a namespace to a subsystem."""
 
        if context:
            assert self.omap_lock.locked()
        nsid_msg = ""
        if nsid and uuid:
            nsid_msg = f" using NSID {nsid} and UUID {uuid}"
        elif nsid:
            nsid_msg = f" using NSID {nsid} "
        elif uuid:
            nsid_msg = f" using UUID {uuid} "

        add_namespace_error_prefix = f"Failure adding namespace{nsid_msg}to {subsystem_nqn}"

        self.logger.info(f"Received request to add {bdev_name} to {subsystem_nqn} with ANA group id {anagrpid}{nsid_msg}")

        if anagrpid > MAX_ANA_GROUPS:
            errmsg = f"{add_namespace_error_prefix}: Group ID {anagrpid} is bigger than configured maximum {MAX_ANA_GROUPS}"
            self.logger.error(errmsg)
            return pb2.nsid_status(status=errno.EINVAL, error_message=errmsg)

        if self.is_discovery_nqn(subsystem_nqn):
            errmsg = f"{add_namespace_error_prefix}: Can't add namespaces to a discovery subsystem"
            self.logger.error(errmsg)
            return pb2.nsid_status(status=errno.EINVAL, error_message=errmsg)

        try:
            nsid = rpc_nvmf.nvmf_subsystem_add_ns(
                self.spdk_rpc_client,
                nqn=subsystem_nqn,
                bdev_name=bdev_name,
                nsid=nsid,
                anagrpid=anagrpid,
                uuid=uuid,
            )
            self.logger.info(f"subsystem_add_ns: {nsid}")
        except Exception as ex:
            errmsg = f"{add_namespace_error_prefix}:\n{ex}"
            self.logger.error(errmsg)
            resp = self.parse_json_exeption(ex)
            status = errno.EINVAL
            if resp:
                status = resp["code"]
                errmsg = f"{add_namespace_error_prefix}: {resp['message']}"
            return pb2.nsid_status(status=status, error_message=errmsg)

        # Just in case SPDK failed with no exception
        if not nsid:
            self.logger.error(add_namespace_error_prefix)
            return pb2.nsid_status(status=errno.EINVAL, error_message=add_namespace_error_prefix)

        return pb2.nsid_status(nsid=nsid, status=0, error_message=os.strerror(0))

    def find_unique_bdev_name(self, uuid) -> str:
        if not uuid:
            uuid = str(uuid.uuid4())
        return f"bdev_{uuid}"

    def get_ns_id_message(self, nsid, uuid):
        ns_id_msg = ""
        if nsid and uuid:
            ns_id_msg = f"using NSID {nsid} and UUID {uuid} "
        elif nsid:
            ns_id_msg = f"using NSID {nsid} "
        elif uuid:
            ns_id_msg = f"using UUID {uuid} "
        return ns_id_msg

    def namespace_add_safe(self, request, context):
        """Adds a namespace to a subsystem."""

        nsid_msg = self.get_ns_id_message(request.nsid, request.uuid)
        self.logger.info(f"Received request to add a namespace {nsid_msg}to {request.subsystem_nqn}, context: {context}")

        if not request.uuid:
            request.uuid = str(uuid.uuid4())

        with self.omap_lock(context=context):
            bdev_name = self.find_unique_bdev_name(request.uuid)

            ret_bdev = self.create_bdev(bdev_name, request.uuid, request.rbd_pool_name, request.rbd_image_name, request.block_size)
            if ret_bdev.status != 0:
                errmsg = f"Failure adding namespace {nsid_msg}to {request.subsystem_nqn}: {ret_bdev.error_message}"
                self.logger.error(errmsg)
                # Delete the bdev just to be on the safe side
                try:
                    ret_del = self.delete_bdev(bdev_name)
                    self.logger.info(f"delete_bdev({bdev_name}): {ret_del.status}")
                except Exception as ex:
                    self.logger.warning(f"Got exception while trying to delete bdev {bdev_name}:\n{ex}")
                return pb2.nsid_status(status=ret_bdev.status, error_message=errmsg)

            if ret_bdev.bdev_name != bdev_name:
                self.logger.warning(f"Returned bdev name {ret_bdev.bdev_name} differs from requested one {bdev_name}")

            ret_ns = self.create_namespace(request.subsystem_nqn, bdev_name, request.nsid, request.anagrpid, request.uuid, context)
            if ret_ns.status != 0:
                try:
                    ret_del = self.delete_bdev(bdev_name)
                    if ret_del.status != 0:
                        self.logger.warning(f"Failure {ret_del.status} deleting bdev {bdev_name}: {ret_del.error_message}")
                except Exception as ex:
                    self.logger.warning(f"Got exception while trying to delete bdev {bdev_name}:\n{ex}")
                errmsg = f"Failure adding namespace {nsid_msg}to {request.subsystem_nqn}:{ret_ns.error_message}"
                self.logger.error(errmsg)
                return pb2.nsid_status(status=ret_ns.status, error_message=errmsg)

            if request.nsid and ret_ns.nsid != request.nsid:
                self.logger.warning(f"Return NSID {ret_ns.nsid} differs from requested one {request.nsid}")

            if context:
                # Update gateway state
                try:
                    json_req = json_format.MessageToJson(
                        request, preserving_proto_field_name=True, including_default_value_fields=True)
                    self.gateway_state.add_namespace(request.subsystem_nqn, ret_ns.nsid, json_req)
                except Exception as ex:
                    errmsg = f"Error persisting namespace {nsid_msg}on {request.subsystem_nqn}:\n{ex}"
                    self.logger.error(errmsg)
                    return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

        return pb2.nsid_status(status=0, error_message=os.strerror(0), nsid=ret_ns.nsid)

    def namespace_add(self, request, context=None):
        """Adds a namespace to a subsystem."""
        return self.execute_grpc_function(self.namespace_add_safe, request, context)

    def namespace_change_load_balancing_group_safe(self, request, context):
        """Changes a namespace load balancing group."""

        nsid_msg = self.get_ns_id_message(request.nsid, request.uuid)
        self.logger.info(f"Received request to change load balancing group for namespace {nsid_msg}in {request.subsystem_nqn} to {request.anagrpid}, context: {context}")

        with self.omap_lock(context=context):
            find_ret = self.find_namespace_and_bdev_name(request.subsystem_nqn, request.nsid, request.uuid, False,
                            f"Failure changing load balancing group for namespace {nsid_msg}in {request.subsystem_nqn}")
            if not find_ret[0]:
                errmsg = f"Failure changing load balancing group for namespace {nsid_msg}in {request.subsystem_nqn}: Can't find namespace"
                self.logger.error(errmsg)
                return pb2.req_status(status=errno.ENODEV, error_message=errmsg)
            try:
                uuid = find_ret[0]["uuid"]
            except KeyError:
                uuid = None
                self.logger.warning(f"Can't get UUID value for namespace {nsid_msg}in {request.subsystem_nqn}:\n{find_ret[0]}")
            try:
                nsid = find_ret[0]["nsid"]
            except KeyError:
                nsid = 0
                self.logger.warning(f"Can't get NSID value for namespace {nsid_msg}in {request.subsystem_nqn}:\n{find_ret[0]}")

            if request.nsid and request.nsid != nsid:
                errmsg = f"Failure changing load balancing group for namespace {nsid_msg}in {request.subsystem_nqn}: Returned NSID {nsid} differs from requested one {request.nsid}"
                self.logger.error(errmsg)
                return pb2.req_status(status=errno.ENODEV, error_message=errmsg)

            if request.uuid and request.uuid != uuid:
                errmsg = f"Failure changing load balancing group for namespace {nsid_msg}in {request.subsystem_nqn}: Returned UUID {uuid} differs from requested one {request.uuid}"
                self.logger.error(errmsg)
                return pb2.req_status(status=errno.ENODEV, error_message=errmsg)

            bdev_name = find_ret[1]
            if not bdev_name:
                bdev_name = self.find_unique_bdev_name(uuid)
                self.logger.warning(f"Failure finding namespace's associated block device name, will use {bdev_name} instead")

            ns_entry = None
            state = self.gateway_state.local.get_state()
            ns_key = GatewayState.build_namespace_key(request.subsystem_nqn, nsid)
            try:
                state_ns = state[ns_key]
                ns_entry = json.loads(state_ns)
            except Exception as ex:
                errmsg = f"Failure changing load balancing group for namespace {nsid_msg}in {request.subsystem_nqn}. Can't get namespace entry from local state:\n{ex}"
                self.logger.error(errmsg)
                return pb2.req_status(status=errno.ENOENT, error_message=errmsg)

            if not ns_entry:
                errmsg = f"Failure changing load balancing group for namespace {nsid_msg}in {request.subsystem_nqn}. Can't get namespace entry from local state"
                self.logger.error(errmsg)
                return pb2.req_status(status=errno.ENOENT, error_message=errmsg)

            ret_del = self.remove_namespace(request.subsystem_nqn, nsid, context)
            if ret_del.status != 0:
                errmsg = f"Failure changing load balancing group for namespace {nsid_msg}in {request.subsystem_nqn}. Can't delete namespace: {ret_del.error_message}"
                self.logger.error(errmsg)
                return pb2.req_status(status=ret_del.status, error_message=errmsg)

            if nsid:
                self.remove_namespace_from_state(request.subsystem_nqn, nsid, context)

            ret_ns = self.create_namespace(request.subsystem_nqn, bdev_name, nsid, request.anagrpid, uuid, context)
            if ret_ns.status != 0:
                errmsg = f"Failure changing load balancing group for namespace {nsid_msg}in {request.subsystem_nqn}:{ret_ns.error_message}"
                self.logger.error(errmsg)
                return pb2.req_status(status=ret_ns.status, error_message=errmsg)

            if context:
                # Update gateway state
                try:
                    namespace_add_req = pb2.namespace_add_req()
                    try:
                        namespace_add_req.rbd_pool_name=ns_entry["rbd_pool_name"]
                    except KeyError:
                        pass
                    try:
                        namespace_add_req.rbd_image_name=ns_entry["rbd_image_name"]
                    except KeyError:
                        pass
                    try:
                        namespace_add_req.subsystem_nqn=ns_entry["subsystem_nqn"]
                    except KeyError:
                        pass
                    try:
                        namespace_add_req.nsid=ns_entry["nsid"]
                    except KeyError:
                        pass
                    try:
                        namespace_add_req.block_size=ns_entry["block_size"]
                    except KeyError:
                        pass
                    try:
                        namespace_add_req.uuid=ns_entry["uuid"]
                    except KeyError:
                        pass
                    namespace_add_req.anagrpid=request.anagrpid
                    json_req = json_format.MessageToJson(
                            namespace_add_req, preserving_proto_field_name=True, including_default_value_fields=True)
                    self.gateway_state.add_namespace(request.subsystem_nqn, nsid, json_req)
                except Exception as ex:
                    errmsg = f"Error persisting change load balancing group for namespace {nsid_msg}in {request.subsystem_nqn}:\n{ex}"
                    self.logger.error(errmsg)
                    return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

        return pb2.req_status(status=0, error_message=os.strerror(0))

    def namespace_change_load_balancing_group(self, request, context=None):
        """Changes a namespace load balancing group."""
        return self.execute_grpc_function(self.namespace_change_load_balancing_group_safe, request, context)

    def remove_namespace_from_state(self, nqn, nsid, context):
        if not context:
            return pb2.req_status(status=0, error_message=os.strerror(0))

        # If we got here context is not None, so we must hold the OMAP lock
        assert self.omap_lock.locked()

        # Update gateway state
        try:
            self.gateway_state.remove_namespace_qos(nqn, str(nsid))
        except Exception as ex:
            self.logger.warning(f"Error removing namespace's QOS limits, they might not have been set")
            pass
        try:
            self.gateway_state.remove_namespace(nqn, str(nsid))
        except Exception as ex:
            errmsg = f"Error persisting removing of namespace {nsid} from {nqn}:\n{ex}"
            self.logger.error(errmsg)
            return pb2.req_status(status=errno.EINVAL, error_message=errmsg)
        return pb2.req_status(status=0, error_message=os.strerror(0))

    def remove_namespace(self, subsystem_nqn, nsid, context):
        """Removes a namespace from a subsystem."""

        if context:
            assert self.omap_lock.locked()
        namespace_failure_prefix = f"Failure removing namespace {nsid} from {subsystem_nqn}"
        self.logger.info(f"Received request to remove namespace {nsid} from {subsystem_nqn}")

        if self.is_discovery_nqn(subsystem_nqn):
            errmsg=f"{namespace_failure_prefix}: Can't remove a namespace from a discovery subsystem"
            self.logger.error(f"{errmsg}")
            return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

        try:
            ret = rpc_nvmf.nvmf_subsystem_remove_ns(
                self.spdk_rpc_client,
                nqn=subsystem_nqn,
                nsid=nsid,
            )
            self.logger.info(f"remove_namespace {nsid}: {ret}")
        except Exception as ex:
            errmsg = f"{namespace_failure_prefix}:\n{ex}"
            self.logger.error(errmsg)
            resp = self.parse_json_exeption(ex)
            status = errno.EINVAL
            if resp:
                status = resp["code"]
                errmsg = f"{namespace_failure_prefix}: {resp['message']}"
            return pb2.req_status(status=status, error_message=errmsg)

        # Just in case SPDK failed with no exception
        if not ret:
            self.logger.error(namespace_failure_prefix)
            return pb2.req_status(status=errno.EINVAL, error_message=namespace_failure_prefix)

        return pb2.req_status(status=0, error_message=os.strerror(0))

    def get_bdev_info(self, bdev_name):
        """Get bdev info"""

        ret_bdev = None
        with self.rpc_lock:
            try:
                bdevs = rpc_bdev.bdev_get_bdevs(self.spdk_rpc_client, name=bdev_name)
                if (len(bdevs) > 1):
                    self.logger.warning(f"Got {len(bdevs)} bdevs for bdev name {bdev_name}, will use the first one")
                ret_bdev = bdevs[0]
            except Exception as ex:
                self.logger.error(f"Got exception while getting bdev {bdev_name} info:\n{ex}")

        return ret_bdev

    def list_namespaces(self, request, context=None):
        """List namespaces."""

        if request.nsid == None or request.nsid == 0:
            if request.uuid:
                nsid_msg = f"namespace with UUID {request.uuid}"
            else:
                nsid_msg = "all namespaces"
        else:
            if request.uuid:
                nsid_msg = f"namespace with NSID {request.nsid} and UUID {request.uuid}"
            else:
                nsid_msg = f"namespace with NSID {request.nsid}"
        self.logger.info(f"Received request to list {nsid_msg} for {request.subsystem}, context: {context}")

        with self.rpc_lock:
            try:
                ret = rpc_nvmf.nvmf_get_subsystems(self.spdk_rpc_client, nqn=request.subsystem)
                self.logger.info(f"list_namespaces: {ret}")
            except Exception as ex:
                errmsg = f"Failure listing namespaces:\n{ex}"
                self.logger.error(f"{errmsg}")
                resp = self.parse_json_exeption(ex)
                status = errno.EINVAL
                if resp:
                    status = resp["code"]
                    errmsg = f"Failure listing namespaces: {resp['message']}"
                return pb2.namespaces_info(status=status, error_message=errmsg, subsystem_nqn=request.subsystem, namespaces=[])

        namespaces = []
        for s in ret:
            try:
                if s["nqn"] != request.subsystem:
                    self.logger.warning(f'Got subsystem {s["nqn"]} instead of {request.subsystem}, ignore')
                    continue
                try:
                    ns_list = s["namespaces"]
                except Exception:
                    ns_list = []
                    pass
                for n in ns_list:
                    if request.nsid and request.nsid != n["nsid"]:
                        self.logger.debug(f'Filter out namespace {n["nsid"]} which is different than requested nsid {request.nsid}')
                        continue
                    if request.uuid and request.uuid != n["uuid"]:
                        self.logger.debug(f'Filter out namespace with UUID {n["uuid"]} which is different than requested UUID {request.uuid}')
                        continue
                    bdev_name = n["bdev_name"]
                    ns_bdev = self.get_bdev_info(bdev_name)
                    lb_group = 0
                    try:
                        lb_group = n["anagrpid"]
                    except KeyError:
                        pass
                    one_ns = pb2.namespace(nsid = n["nsid"],
                                           bdev_name = bdev_name,
                                           uuid = n["uuid"],
                                           load_balancing_group = lb_group)
                    if ns_bdev == None:
                        self.logger.warning(f"Can't find namespace's bdev {bdev_name}, will not list bdev's information")
                    else:
                        try:
                            drv_specific_info = ns_bdev["driver_specific"]
                            rbd_info = drv_specific_info["rbd"]
                            one_ns.rbd_image_name = rbd_info["rbd_name"]
                            one_ns.rbd_pool_name = rbd_info["pool_name"]
                            one_ns.block_size = ns_bdev["block_size"]
                            one_ns.rbd_image_size = ns_bdev["block_size"] * ns_bdev["num_blocks"]
                            assigned_limits = ns_bdev["assigned_rate_limits"]
                            one_ns.rw_ios_per_second=assigned_limits["rw_ios_per_sec"]
                            one_ns.rw_mbytes_per_second=assigned_limits["rw_mbytes_per_sec"]
                            one_ns.r_mbytes_per_second=assigned_limits["r_mbytes_per_sec"]
                            one_ns.w_mbytes_per_second=assigned_limits["w_mbytes_per_sec"]
                        except KeyError as err:
                            self.logger.warning(f"Key {err} is not found, will not list bdev's information") 
                            pass
                        except Exception:
                            self.logger.exception(f"{ns_bdev=} parse error: ") 
                            pass
                    namespaces.append(one_ns)
                break
            except Exception:
                self.logger.exception(f"{s=} parse error: ")
                pass

        return pb2.namespaces_info(status = 0, error_message = os.strerror(0), subsystem_nqn=request.subsystem, namespaces=namespaces)

    def namespace_get_io_stats(self, request, context=None):
        """Get namespace's IO stats."""

        nsid_msg = self.get_ns_id_message(request.nsid, request.uuid)
        self.logger.info(f"Received request to get IO stats for namespace {nsid_msg}on {request.subsystem_nqn}, context: {context}")

        with self.rpc_lock:
            find_ret = self.find_namespace_and_bdev_name(request.subsystem_nqn, request.nsid, request.uuid, False,
                                                     "Failure getting namespace's IO stats")
            ns = find_ret[0]
            if not ns:
                errmsg = f"Failure getting IO stats for namespace {nsid_msg}on {request.subsystem_nqn}: Can't find namespace"
                self.logger.error(errmsg)
                return pb2.namespace_io_stats_info(status=errno.ENODEV, error_message=errmsg)
            bdev_name = find_ret[1]
            if not bdev_name:
                errmsg = f"Failure getting IO stats for namespace {nsid_msg}on {request.subsystem_nqn}: Can't find associated block device"
                self.logger.error(errmsg)
                return pb2.namespace_io_stats_info(status=errno.ENODEV, error_message=errmsg)

            try:
                ret = rpc_bdev.bdev_get_iostat(
                    self.spdk_rpc_client,
                    name=bdev_name,
                )
                self.logger.info(f"get_bdev_iostat {bdev_name}: {ret}")
            except Exception as ex:
                errmsg = f"Failure getting IO stats for namespace {nsid_msg}on {request.subsystem_nqn}:\n{ex}"
                self.logger.error(errmsg)
                resp = self.parse_json_exeption(ex)
                status = errno.EINVAL
                if resp:
                    status = resp["code"]
                    errmsg = f"Failure getting IO stats for namespace {nsid_msg}on {request.subsystem_nqn}: {resp['message']}"
                return pb2.namespace_io_stats_info(status=status, error_message=errmsg)

        # Just in case SPDK failed with no exception
        if not ret:
            errmsg = f"Failure getting IO stats for namespace {nsid_msg}on {request.subsystem_nqn}"
            self.logger.error(errmsg)
            return pb2.namespace_io_stats_info(status=errno.EINVAL, error_message=errmsg)

        exmsg = ""
        try:
            bdevs = ret["bdevs"]
            if not bdevs:
                return pb2.namespace_io_stats_info(status=errno.ENODEV,
                                                   error_message=f"Failure getting IO stats for namespace {nsid_msg}on {request.subsystem_nqn}: No associated block device found")
            if len(bdevs) > 1:
                self.logger.warning(f"More than one associated block device found for namespace, will use the first one")
            bdev = bdevs[0]
            io_stats = pb2.namespace_io_stats_info(status=0,
                               error_message=os.strerror(0),
                               subsystem_nqn=request.subsystem_nqn,
                               nsid=ns["nsid"],
                               uuid=ns["uuid"],
                               bdev_name=bdev_name,
                               tick_rate=ret["tick_rate"],
                               ticks=ret["ticks"],
                               bytes_read=bdev["bytes_read"],
                               num_read_ops=bdev["num_read_ops"],
                               bytes_written=bdev["bytes_written"],
                               num_write_ops=bdev["num_write_ops"],
                               bytes_unmapped=bdev["bytes_unmapped"],
                               num_unmap_ops=bdev["num_unmap_ops"],
                               read_latency_ticks=bdev["read_latency_ticks"],
                               max_read_latency_ticks=bdev["max_read_latency_ticks"],
                               min_read_latency_ticks=bdev["min_read_latency_ticks"],
                               write_latency_ticks=bdev["write_latency_ticks"],
                               max_write_latency_ticks=bdev["max_write_latency_ticks"],
                               min_write_latency_ticks=bdev["min_write_latency_ticks"],
                               unmap_latency_ticks=bdev["unmap_latency_ticks"],
                               max_unmap_latency_ticks=bdev["max_unmap_latency_ticks"],
                               min_unmap_latency_ticks=bdev["min_unmap_latency_ticks"],
                               copy_latency_ticks=bdev["copy_latency_ticks"],
                               max_copy_latency_ticks=bdev["max_copy_latency_ticks"],
                               min_copy_latency_ticks=bdev["min_copy_latency_ticks"],
                               io_error=bdev["io_error"])
            return io_stats
        except Exception as ex:
            self.logger.exception(f"{s=} parse error: ")
            exmsg = str(ex)
            pass

        return pb2.namespace_io_stats_info(status=errno.EINVAL,
                               error_message=f"Failure getting IO stats for namespace {nsid_msg}on {request.subsystem_nqn}: Error parsing returned stats:\n{exmsg}") 

    def get_qos_limits_string(self, request):
        limits_to_set = ""
        if request.HasField("rw_ios_per_second"):
            limits_to_set += f" R/W IOs per second: {request.rw_ios_per_second}"
        if request.HasField("rw_mbytes_per_second"):
            limits_to_set += f" R/W megabytes per second: {request.rw_mbytes_per_second}"
        if request.HasField("r_mbytes_per_second"):
            limits_to_set += f" Read megabytes per second: {request.r_mbytes_per_second}"
        if request.HasField("w_mbytes_per_second"):
            limits_to_set += f" Write megabytes per second: {request.w_mbytes_per_second}"

        return limits_to_set

    def namespace_set_qos_limits_safe(self, request, context):
        """Set namespace's qos limits."""

        nsid_msg = self.get_ns_id_message(request.nsid, request.uuid)
        limits_to_set = self.get_qos_limits_string(request)
        self.logger.info(f"Received request to set QOS limits for namespace {nsid_msg}on {request.subsystem_nqn},{limits_to_set}, context: {context}")

        find_ret = self.find_namespace_and_bdev_name(request.subsystem_nqn, request.nsid, request.uuid, False,
                                                 "Failure setting namespace's QOS limits")
        if not find_ret[0]:
            errmsg = f"Failure setting QOS limits for namespace {nsid_msg}on {request.subsystem_nqn}: Can't find namespace"
            self.logger.error(errmsg)
            return pb2.req_status(status=errno.ENODEV, error_message=errmsg)
        bdev_name = find_ret[1]
        if not bdev_name:
            errmsg = f"Failure setting QOS limits for namespace {nsid_msg}on {request.subsystem_nqn}: Can't find associated block device"
            self.logger.error(errmsg)
            return pb2.req_status(status=errno.ENODEV, error_message=errmsg)
        nsid = find_ret[0]["nsid"]

        set_qos_limits_args = {}
        set_qos_limits_args["name"] = bdev_name
        if request.HasField("rw_ios_per_second"):
            set_qos_limits_args["rw_ios_per_sec"] = request.rw_ios_per_second
        if request.HasField("rw_mbytes_per_second"):
            set_qos_limits_args["rw_mbytes_per_sec"] = request.rw_mbytes_per_second
        if request.HasField("r_mbytes_per_second"):
            set_qos_limits_args["r_mbytes_per_sec"] = request.r_mbytes_per_second
        if request.HasField("w_mbytes_per_second"):
            set_qos_limits_args["w_mbytes_per_sec"] = request.w_mbytes_per_second

        ns_qos_entry = None
        if context:
            state = self.gateway_state.local.get_state()
            ns_qos_key = GatewayState.build_namespace_qos_key(request.subsystem_nqn, nsid)
            try:
                state_ns_qos = state[ns_qos_key]
                ns_qos_entry = json.loads(state_ns_qos)
            except Exception as ex:
                self.logger.info(f"No previous QOS limits found, this is the first time the limits are set for namespace {nsid_msg}on {request.subsystem_nqn}")

        # Merge current limits with previous ones, if exist
        if ns_qos_entry:
            if not request.HasField("rw_ios_per_second") and ns_qos_entry.get("rw_ios_per_second") != None:
                request.rw_ios_per_second = int(ns_qos_entry["rw_ios_per_second"])
            if not request.HasField("rw_mbytes_per_second") and ns_qos_entry.get("rw_mbytes_per_second") != None:
                request.rw_mbytes_per_second = int(ns_qos_entry["rw_mbytes_per_second"])
            if not request.HasField("r_mbytes_per_second") and ns_qos_entry.get("r_mbytes_per_second") != None:
                request.r_mbytes_per_second = int(ns_qos_entry["r_mbytes_per_second"])
            if not request.HasField("w_mbytes_per_second") and ns_qos_entry.get("w_mbytes_per_second") != None:
                request.w_mbytes_per_second = int(ns_qos_entry["w_mbytes_per_second"])

            limits_to_set = self.get_qos_limits_string(request)
            self.logger.info(f"After merging current QOS limits with previous ones for namespace {nsid_msg}on {request.subsystem_nqn},{limits_to_set}")

        with self.omap_lock(context=context):
            try:
                ret = rpc_bdev.bdev_set_qos_limit(
                    self.spdk_rpc_client,
                    **set_qos_limits_args)
                self.logger.info(f"bdev_set_qos_limit {bdev_name}: {ret}")
            except Exception as ex:
                errmsg = f"Failure setting QOS limits for namespace {nsid_msg}on {request.subsystem_nqn}:\n{ex}"
                self.logger.error(errmsg)
                resp = self.parse_json_exeption(ex)
                status = errno.EINVAL
                if resp:
                    status = resp["code"]
                    errmsg = f"Failure setting namespace's QOS limits: {resp['message']}"
                return pb2.req_status(status=status, error_message=errmsg)

            # Just in case SPDK failed with no exception
            if not ret:
                errmsg = f"Failure setting QOS limits for namespace {nsid_msg}on {request.subsystem_nqn}"
                self.logger.error(errmsg)
                return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

            if context:
                # Update gateway state
                try:
                    json_req = json_format.MessageToJson(
                        request, preserving_proto_field_name=True, including_default_value_fields=True)
                    self.gateway_state.add_namespace_qos(request.subsystem_nqn, nsid, json_req)
                except Exception as ex:
                    errmsg = f"Error persisting namespace QOS settings {nsid_msg}on {request.subsystem_nqn}:\n{ex}"
                    self.logger.error(errmsg)
                    return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

        return pb2.req_status(status=0, error_message=os.strerror(0))

    def namespace_set_qos_limits(self, request, context=None):
        """Set namespace's qos limits."""
        return self.execute_grpc_function(self.namespace_set_qos_limits_safe, request, context)

    def find_namespace_and_bdev_name(self, nqn, nsid, uuid, needs_lock, err_prefix):
        if nsid <= 0 and not uuid:
           self.logger.error(f"{err_prefix}: At least one of NSID or UUID should be specified for finding a namesapce")
           return (None, None)

        if needs_lock:
            lock_to_use = self.rpc_lock
        else:
            lock_to_use = contextlib.suppress()

        with lock_to_use:
            try:
                ret = rpc_nvmf.nvmf_get_subsystems(self.spdk_rpc_client, nqn=nqn)
                self.logger.info(f"find_namespace_and_bdev_name: {ret}")
            except Exception as ex:
                errmsg = f"{err_prefix}:\n{ex}"
                self.logger.error(errmsg)
                return (None, None)

        if not ret:
           return (None, None)

        bdev_name = None
        found_ns = None
        for s in ret:
            try:
                if s["nqn"] != nqn:
                    self.logger.warning(f'Got subsystem {s["nqn"]} instead of {nqn}, ignore')
                    continue
                try:
                    ns_list = s["namespaces"]
                except Exception:
                    ns_list = []
                    pass
                for n in ns_list:
                    if nsid > 0 and nsid != n["nsid"]:
                        continue
                    if uuid and uuid != n["uuid"]:
                        continue
                    found_ns = n
                    bdev_name = n["bdev_name"]
                break
            except Exception:
                self.logger.exception(f"{s=} parse error: ")
                pass

        return (found_ns, bdev_name)

    def namespace_resize(self, request, context=None):
        """Resize a namespace."""

        nsid_msg = self.get_ns_id_message(request.nsid, request.uuid)
        self.logger.info(f"Received request to resize namespace {nsid_msg}on {request.subsystem_nqn} to {request.new_size} MiB, context: {context}")

        find_ret = self.find_namespace_and_bdev_name(request.subsystem_nqn, request.nsid, request.uuid, True, "Failure resizing namespace")
        if not find_ret[0]:
            errmsg = f"Failure resizing namespace {nsid_msg}on {request.subsystem_nqn}: Can't find namespace"
            self.logger.error(errmsg)
            return pb2.req_status(status=errno.ENODEV, error_message=errmsg)
        bdev_name = find_ret[1]
        if not bdev_name:
            errmsg = f"Failure resizing namespace {nsid_msg}on {request.subsystem_nqn}: Can't find associated block device"
            self.logger.error(errmsg)
            return pb2.req_status(status=errno.ENODEV, error_message=errmsg)

        ret = self.resize_bdev(bdev_name, request.new_size)

        if ret.status == 0:
            errmsg = os.strerror(0)
        else:
            errmsg = f"Failure resizing namespace: {ret.error_message}"
            self.logger.error(errmsg)

        return pb2.req_status(status=ret.status, error_message=errmsg)

    def namespace_delete_safe(self, request, context):
        """Delete a namespace."""

        nsid_msg = self.get_ns_id_message(request.nsid, request.uuid)
        self.logger.info(f"Received request to delete namespace {nsid_msg}from {request.subsystem_nqn}, context: {context}")

        with self.omap_lock(context=context):
            find_ret = self.find_namespace_and_bdev_name(request.subsystem_nqn, request.nsid, request.uuid, False,
                                                         "Failure deleting namespace")
            if not find_ret[0]:
                errmsg = f"Failure deleting namespace: Can't find namespace"
                self.logger.error(errmsg)
                return pb2.req_status(status=errno.ENODEV, error_message=errmsg)
            bdev_name = find_ret[1]
            if not bdev_name:
                self.logger.warning(f"Can't find namespace's bdev name, will try to delete namespace anyway")

            ns = find_ret[0]
            nsid = ns["nsid"]
            ret = self.remove_namespace(request.subsystem_nqn, nsid, context)
            if ret.status != 0:
                return ret

            self.remove_namespace_from_state(request.subsystem_nqn, nsid, context)
            if bdev_name:
                ret_del = self.delete_bdev(bdev_name)
                if ret_del.status != 0:
                    errmsg = f"Failure deleting namespace {nsid_msg}from {request.subsystem_nqn}: {ret_del.error_message}"
                    self.logger.error(errmsg)
                    return pb2.nsid_status(status=ret_del.status, error_message=errmsg)

        return pb2.req_status(status=0, error_message=os.strerror(0))

    def namespace_delete(self, request, context=None):
        """Delete a namespace."""
        return self.execute_grpc_function(self.namespace_delete_safe, request, context)

    def matching_host_exists(self, context, subsys_nqn, host_nqn) -> bool:
        if not context:
            return False
        host_key = GatewayState.build_host_key(subsys_nqn, host_nqn)
        state = self.gateway_state.local.get_state()
        if state.get(host_key):
            return True
        else:
            return False

    def add_host_safe(self, request, context):
        """Adds a host to a subsystem."""

        all_host_failure_prefix=f"Failure allowing open host access to {request.subsystem_nqn}"
        host_failure_prefix=f"Failure adding host {request.host_nqn} to {request.subsystem_nqn}"

        if self.is_discovery_nqn(request.subsystem_nqn):
            if request.host_nqn == "*":
                errmsg=f"{all_host_failure_prefix}: Can't allow host access to a discovery subsystem"
            else:
                errmsg=f"{host_failure_prefix}: Can't add host to a discovery subsystem"
            self.logger.error(f"{errmsg}")
            return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

        if self.is_discovery_nqn(request.host_nqn):
            errmsg=f"{host_failure_prefix}: Can't use a discovery NQN as host's"
            self.logger.error(f"{errmsg}")
            return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

        with self.omap_lock(context=context):
            try:
                host_already_exist = self.matching_host_exists(context, request.subsystem_nqn, request.host_nqn)
                if host_already_exist:
                    if request.host_nqn == "*":
                        errmsg = f"{all_host_failure_prefix}: Open host access is already allowed"
                        self.logger.error(f"{errmsg}")
                        return pb2.req_status(status=errno.EEXIST, error_message=errmsg)
                    else:
                        errmsg = f"{host_failure_prefix}: Host is already added"
                        self.logger.error(f"{errmsg}")
                        return pb2.req_status(status=errno.EEXIST, error_message=errmsg)

                if request.host_nqn == "*":  # Allow any host access to subsystem
                    self.logger.info(f"Received request to allow any host access for {request.subsystem_nqn}, context: {context}")
                    ret = rpc_nvmf.nvmf_subsystem_allow_any_host(
                        self.spdk_rpc_client,
                        nqn=request.subsystem_nqn,
                        disable=False,
                    )
                    self.logger.info(f"add_host *: {ret}")
                else:  # Allow single host access to subsystem
                    self.logger.info(
                        f"Received request to add host {request.host_nqn} to {request.subsystem_nqn}, context: {context}")
                    ret = rpc_nvmf.nvmf_subsystem_add_host(
                        self.spdk_rpc_client,
                        nqn=request.subsystem_nqn,
                        host=request.host_nqn,
                    )
                    self.logger.info(f"add_host {request.host_nqn}: {ret}")
            except Exception as ex:
                if request.host_nqn == "*":
                    errmsg = f"{all_host_failure_prefix}:\n{ex}"
                else:
                    errmsg = f"{host_failure_prefix}:\n{ex}"
                self.logger.error(errmsg)
                resp = self.parse_json_exeption(ex)
                status = errno.EINVAL
                if resp:
                    status = resp["code"]
                    if request.host_nqn == "*":
                        errmsg = f"{all_host_failure_prefix}: {resp['message']}"
                    else:
                        errmsg = f"{host_failure_prefix}: {resp['message']}"
                return pb2.req_status(status=status, error_message=errmsg)

            # Just in case SPDK failed with no exception
            if not ret:
                if request.host_nqn == "*":
                    errmsg = all_host_failure_prefix
                else:
                    errmsg = host_failure_prefix
                self.logger.error(errmsg)
                return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

            if context:
                # Update gateway state
                try:
                    json_req = json_format.MessageToJson(
                        request, preserving_proto_field_name=True, including_default_value_fields=True)
                    self.gateway_state.add_host(request.subsystem_nqn,
                                                request.host_nqn, json_req)
                except Exception as ex:
                    errmsg = f"Error persisting host {request.host_nqn} access addition:\n{ex}"
                    self.logger.error(errmsg)
                    return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

        return pb2.req_status(status=0, error_message=os.strerror(0))

    def add_host(self, request, context=None):
        return self.execute_grpc_function(self.add_host_safe, request, context)

    def remove_host_from_state(self, subsystem_nqn, host_nqn, context):
        if not context:
            return pb2.req_status(status=0, error_message=os.strerror(0))

        if context:
            assert self.omap_lock.locked()
        # Update gateway state
        try:
            self.gateway_state.remove_host(subsystem_nqn, host_nqn)
        except Exception as ex:
            errmsg = f"Error persisting host {host_nqn} access removal:\n{ex}"
            self.logger.error(errmsg)
            return pb2.req_status(status=errno.EINVAL, error_message=errmsg)
        return pb2.req_status(status=0, error_message=os.strerror(0))

    def remove_host_safe(self, request, context):
        """Removes a host from a subsystem."""

        all_host_failure_prefix=f"Failure disabling open host access to {request.subsystem_nqn}"
        host_failure_prefix=f"Failure removing host {request.host_nqn} access from {request.subsystem_nqn}"

        if self.is_discovery_nqn(request.subsystem_nqn):
            if request.host_nqn == "*":
                errmsg=f"{all_host_failure_prefix}: Can't disable open host access to a discovery subsystem"
            else:
                errmsg=f"{host_failure_prefix}: Can't remove host access from a discovery subsystem"
            self.logger.error(f"{errmsg}")
            return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

        if self.is_discovery_nqn(request.host_nqn):
            if request.host_nqn == "*":
                errmsg=f"{all_host_failure_prefix}: Can't use a discovery NQN as host's"
            else:
                errmsg=f"{host_failure_prefix}: Can't use a discovery NQN as host's"
            self.logger.error(f"{errmsg}")
            return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

        with self.omap_lock(context=context):
            try:
                if request.host_nqn == "*":  # Disable allow any host access
                    self.logger.info(
                        f"Received request to disable open host access to"
                        f" {request.subsystem_nqn}, context: {context}")
                    ret = rpc_nvmf.nvmf_subsystem_allow_any_host(
                        self.spdk_rpc_client,
                        nqn=request.subsystem_nqn,
                        disable=True,
                    )
                    self.logger.info(f"remove_host *: {ret}")
                else:  # Remove single host access to subsystem
                    self.logger.info(
                        f"Received request to remove host {request.host_nqn} access from"
                        f" {request.subsystem_nqn}, context: {context}")
                    ret = rpc_nvmf.nvmf_subsystem_remove_host(
                        self.spdk_rpc_client,
                        nqn=request.subsystem_nqn,
                        host=request.host_nqn,
                    )
                    self.logger.info(f"remove_host {request.host_nqn}: {ret}")
            except Exception as ex:
                if request.host_nqn == "*":
                    errmsg = f"{all_host_failure_prefix}:\n{ex}"
                else:
                    errmsg = f"{host_failure_prefix}:\n{ex}"
                self.logger.error(errmsg)
                self.remove_host_from_state(request.subsystem_nqn, request.host_nqn, context)
                resp = self.parse_json_exeption(ex)
                status = errno.EINVAL
                if resp:
                    status = resp["code"]
                    if request.host_nqn == "*":
                        errmsg = f"{all_host_failure_prefix}: {resp['message']}"
                    else:
                        errmsg = f"{host_failure_prefix}: {resp['message']}"
                return pb2.req_status(status=status, error_message=errmsg)

            # Just in case SPDK failed with no exception
            if not ret:
                if request.host_nqn == "*":
                    errmsg = all_host_failure_prefix
                else:
                    errmsg = host_failure_prefix
                self.logger.error(errmsg)
                self.remove_host_from_state(request.subsystem_nqn, request.host_nqn, context)
                return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

            return self.remove_host_from_state(request.subsystem_nqn, request.host_nqn, context)

    def remove_host(self, request, context=None):
        return self.execute_grpc_function(self.remove_host_safe, request, context)

    def list_hosts_safe(self, request, context):
        """List hosts."""

        self.logger.info(f"Received request to list hosts for {request.subsystem}, context: {context}")
        try:
            ret = rpc_nvmf.nvmf_get_subsystems(self.spdk_rpc_client, nqn=request.subsystem)
            self.logger.info(f"list_hosts: {ret}")
        except Exception as ex:
            errmsg = f"Failure listing hosts, can't get subsystems:\n{ex}"
            self.logger.error(f"{errmsg}")
            resp = self.parse_json_exeption(ex)
            status = errno.EINVAL
            if resp:
                status = resp["code"]
                errmsg = f"Failure listing hosts, can't get subsystems: {resp['message']}"
            return pb2.hosts_info(status=status, error_message=errmsg, hosts=[])

        hosts = []
        allow_any_host = False
        for s in ret:
            try:
                if s["nqn"] != request.subsystem:
                    self.logger.warning(f'Got subsystem {s["nqn"]} instead of {request.subsystem}, ignore')
                    continue
                try:
                    allow_any_host = s["allow_any_host"]
                    host_nqns = s["hosts"]
                except Exception:
                    host_nqns = []
                    pass
                for h in host_nqns:
                    one_host = pb2.host(nqn = h["nqn"])
                    hosts.append(one_host)
                break
            except Exception:
                self.logger.exception(f"{s=} parse error: ")
                pass

        return pb2.hosts_info(status = 0, error_message = os.strerror(0), allow_any_host=allow_any_host,
                              subsystem_nqn=request.subsystem, hosts=hosts)

    def list_hosts(self, request, context=None):
        return self.execute_grpc_function(self.list_hosts_safe, request, context)

    def list_connections_safe(self, request, context):
        """List connections."""

        self.logger.info(f"Received request to list connections for {request.subsystem}, context: {context}")
        try:
            qpair_ret = rpc_nvmf.nvmf_subsystem_get_qpairs(self.spdk_rpc_client, nqn=request.subsystem)
            self.logger.info(f"list_connections get_qpairs: {qpair_ret}")
        except Exception as ex:
            errmsg = f"Failure listing connections, can't get qpairs:\n{ex}"
            self.logger.error(f"{errmsg}")
            resp = self.parse_json_exeption(ex)
            status = errno.EINVAL
            if resp:
                status = resp["code"]
                errmsg = f"Failure listing connections, can't get qpairs: {resp['message']}"
            return pb2.connections_info(status=status, error_message=errmsg, connections=[])

        try:
            ctrl_ret = rpc_nvmf.nvmf_subsystem_get_controllers(self.spdk_rpc_client, nqn=request.subsystem)
            self.logger.info(f"list_connections get_controllers: {ctrl_ret}")
        except Exception as ex:
            errmsg = f"Failure listing connections, can't get controllers:\n{ex}"
            self.logger.error(f"{errmsg}")
            resp = self.parse_json_exeption(ex)
            status = errno.EINVAL
            if resp:
                status = resp["code"]
                errmsg = f"Failure listing connections, can't get controllers: {resp['message']}"
            return pb2.bconnections_info(status=status, error_message=errmsg, connections=[])

        try:
            subsys_ret = rpc_nvmf.nvmf_get_subsystems(self.spdk_rpc_client, nqn=request.subsystem)
            self.logger.info(f"list_connections subsystems: {subsys_ret}")
        except Exception as ex:
            errmsg = f"Failure listing connections, can't get subsystems:\n{ex}"
            self.logger.error(f"{errmsg}")
            resp = self.parse_json_exeption(ex)
            status = errno.EINVAL
            if resp:
                status = resp["code"]
                errmsg = f"Failure listing connections, can't get subsystems: {resp['message']}"
            return pb2.connections_info(status=status, error_message=errmsg, connections=[])

        connections = []
        host_nqns = []
        for s in subsys_ret:
            try:
                if s["nqn"] != request.subsystem:
                    self.logger.warning(f'Got subsystem {s["nqn"]} instead of {request.subsystem}, ignore')
                    continue
                try:
                    subsys_hosts = s["hosts"]
                except Exception:
                    subsys_hosts = []
                    pass
                for h in subsys_hosts:
                    try:
                        host_nqns.append(h["nqn"])
                    except Exception:
                        pass
                break
            except Exception:
                self.logger.exception(f"{s=} parse error: ")
                pass

        for conn in ctrl_ret:
            try:
                traddr = ""
                trsvcid = 0
                adrfam = ""
                trtype = ""
                hostnqn = conn["hostnqn"]
                connected = False

                for qp in qpair_ret:
                    try:
                        if qp["cntlid"] != conn["cntlid"]:
                            continue
                        if qp["state"] != "active":
                            continue
                        addr = qp["listen_address"]
                        traddr = addr["traddr"]
                        trsvcid = int(addr["trsvcid"])
                        try:
                            trtype = addr["trtype"].upper()
                        except Exception:
                            pass
                        try:
                            adrfam = addr["adrfam"].lower()
                        except Exception:
                            pass
                        break
                    except Exception as ex:
                        self.logger.warning(f"Got exception while parsing qpair: {qp}:\n{ex}")
                        pass
                one_conn = pb2.connection(nqn=hostnqn, connected=True,
                                          traddr=traddr, trsvcid=trsvcid, trtype=trtype, adrfam=adrfam,
                                          qpairs_count=conn["num_io_qpairs"], controller_id=conn["cntlid"])
                connections.append(one_conn)
                host_nqns.remove(hostnqn)
            except Exception:
                self.logger.exception(f"{s=} parse error: ")
                pass

        for nqn in host_nqns:
            one_conn = pb2.connection(nqn=nqn, connected=False, traddr="<n/a>", trsvcid=0,
                                      qpairs_count=-1, controller_id=-1)
            connections.append(one_conn)

        return pb2.connections_info(status = 0, error_message = os.strerror(0),
                              subsystem_nqn=request.subsystem, connections=connections)

    def list_connections(self, request, context=None):
        return self.execute_grpc_function(self.list_connections_safe, request, context)

    def get_subsystem_ha_status(self, nqn) -> bool:
        enable_ha = False
        state = self.gateway_state.local.get_state()
        subsys_str = state.get(GatewayState.build_subsystem_key(nqn))
        if subsys_str:
            self.logger.debug(f"value of sub-system: {subsys_str}")
            try:
                subsys_dict = json.loads(subsys_str)
                try:
                    enable_ha = subsys_dict["enable_ha"]
                except KeyError:
                    enable_ha = False
                self.logger.info(f"Subsystem {nqn} enable_ha: {enable_ha}")
            except Exception as ex:
                self.logger.error(f"Got exception trying to parse subsystem {nqn}:\n{ex}")
                enable_ha = False
                pass
        else:
            self.logger.warning(f"Subsystem {nqn} not found")
        return enable_ha

    def matching_listener_exists(self, context, nqn, gw_name, trtype, traddr, trsvcid) -> bool:
        if not context:
            return False
        listener_key = GatewayState.build_listener_key(nqn, gw_name, trtype, traddr, trsvcid)
        state = self.gateway_state.local.get_state()
        if state.get(listener_key):
            return True
        else:
            return False

    def create_listener_safe(self, request, context):
        """Creates a listener for a subsystem at a given IP/Port."""

        ret = True
        traddr = GatewayConfig.escape_address_if_ipv6(request.traddr)
        create_listener_error_prefix = f"Failure adding {request.nqn} listener at {traddr}:{request.trsvcid}"

        trtype = GatewayEnumUtils.get_key_from_value(pb2.TransportType, request.trtype)
        if trtype == None:
            errmsg=f"{create_listener_error_prefix}: Unknown transport type {request.trtype}"
            self.logger.error(f"{errmsg}")
            return pb2.req_status(status=errno.ENOKEY, error_message=errmsg)

        adrfam = GatewayEnumUtils.get_key_from_value(pb2.AddressFamily, request.adrfam)
        if adrfam == None:
            errmsg=f"{create_listener_error_prefix}: Unknown address family {request.adrfam}"
            self.logger.error(f"{errmsg}")
            return pb2.req_status(status=errno.ENOKEY, error_message=errmsg)

        auto_ha_state = GatewayEnumUtils.get_key_from_value(pb2.AutoHAState, request.auto_ha_state)
        if auto_ha_state == None:
            errmsg=f"{create_listener_error_prefix}: Unknown auto HA state {request.auto_ha_state}"
            self.logger.error(f"{errmsg}")
            return pb2.req_status(status=errno.ENOKEY, error_message=errmsg)

        self.logger.info(f"Received request to create {request.gateway_name}"
                         f" {trtype} {adrfam} listener for {request.nqn} at"
                         f" {traddr}:{request.trsvcid}, auto HA state: {auto_ha_state}, context: {context}")

        if self.is_discovery_nqn(request.nqn):
            errmsg=f"{create_listener_error_prefix}: Can't create a listener for a discovery subsystem"
            self.logger.error(f"{errmsg}")
            return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

        with self.omap_lock(context=context):
            try:
                if request.gateway_name == self.gateway_name:
                    listener_already_exist = self.matching_listener_exists(
                            context, request.nqn, request.gateway_name, trtype, request.traddr, request.trsvcid)
                    if listener_already_exist:
                        self.logger.error(f"{request.nqn} already listens on address {traddr}:{request.trsvcid}")
                        return pb2.req_status(status=errno.EEXIST,
                                  error_message=f"{create_listener_error_prefix}: Subsystem already listens on this address")
                    ret = rpc_nvmf.nvmf_subsystem_add_listener(
                        self.spdk_rpc_client,
                        nqn=request.nqn,
                        trtype=trtype,
                        traddr=request.traddr,
                        trsvcid=str(request.trsvcid),
                        adrfam=adrfam,
                    )
                    self.logger.info(f"create_listener: {ret}")
                else:
                    errmsg=f"{create_listener_error_prefix}: Gateway name must match current gateway ({self.gateway_name})"
                    self.logger.error(f"{errmsg}")
                    return pb2.req_status(status=errno.ENOENT,
                                  error_message=errmsg)
            except Exception as ex:
                errmsg = f"{create_listener_error_prefix}:\n{ex}"
                self.logger.error(errmsg)
                resp = self.parse_json_exeption(ex)
                status = errno.EINVAL
                if resp:
                    status = resp["code"]
                    errmsg = f"{create_listener_error_prefix}: {resp['message']}"
                return pb2.req_status(status=status, error_message=errmsg)

            # Just in case SPDK failed with no exception
            if not ret:
                self.logger.error(create_listener_error_prefix)
                return pb2.req_status(status=errno.EINVAL, error_message=create_listener_error_prefix)

            enable_ha = False
            if auto_ha_state == "AUTO_HA_UNSET":
                if context == None:
                    self.logger.error(f"auto_ha_state is not set but we are in an update()")
                state = self.gateway_state.local.get_state()
                subsys_str = state.get(GatewayState.build_subsystem_key(request.nqn))
                if subsys_str:
                    self.logger.debug(f"value of sub-system: {subsys_str}")
                    try:
                        subsys_dict = json.loads(subsys_str)
                        try:
                            enable_ha = subsys_dict["enable_ha"]
                            auto_ha_state_key = "AUTO_HA_ON" if enable_ha else "AUTO_HA_OFF"
                            request.auto_ha_state = GatewayEnumUtils.get_value_from_key(pb2.AutoHAState, auto_ha_state_key)
                        except KeyError:
                            enable_ha = False
                        self.logger.info(f"enable_ha: {enable_ha}")
                    except Exception as ex:
                        self.logger.error(f"Got exception trying to parse subsystem {request.nqn}:\n{ex}")
                        pass
                else:
                    self.logger.warning(f"No subsystem for {request.nqn}")
            else:
                if context != None:
                    self.logger.error(f"auto_ha_state is set to {auto_ha_state} but we are not in an update()")
                if auto_ha_state == "AUTO_HA_OFF":
                    enable_ha = False
                elif auto_ha_state == "AUTO_HA_ON":
                    enable_ha = True

            if enable_ha:
                  for x in range (MAX_ANA_GROUPS):
                       try:
                          ret = rpc_nvmf.nvmf_subsystem_listener_set_ana_state(
                            self.spdk_rpc_client,
                            nqn=request.nqn,
                            ana_state="inaccessible",
                            trtype=trtype,
                            traddr=request.traddr,
                            trsvcid=str(request.trsvcid),
                            adrfam=adrfam,
                            anagrpid=(x+1) )
                       except Exception as ex:
                            errmsg=f"{create_listener_error_prefix}: Error setting ANA state:\n{ex}"
                            self.logger.error(errmsg)
                            resp = self.parse_json_exeption(ex)
                            status = errno.EINVAL
                            if resp:
                                status = resp["code"]
                                errmsg = f"{create_listener_error_prefix}: Error setting ANA state: {resp['message']}"
                            return pb2.req_status(status=status, error_message=errmsg)

            if context:
                # Update gateway state
                try:
                    json_req = json_format.MessageToJson(
                        request, preserving_proto_field_name=True, including_default_value_fields=True)
                    self.gateway_state.add_listener(request.nqn,
                                                    request.gateway_name,
                                                    trtype, request.traddr,
                                                    request.trsvcid, json_req)
                except Exception as ex:
                    errmsg = f"Error persisting listener {traddr}:{request.trsvcid}:\n{ex}"
                    self.logger.error(errmsg)
                    return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

        return pb2.req_status(status=0, error_message=os.strerror(0))

    def create_listener(self, request, context=None):
        return self.execute_grpc_function(self.create_listener_safe, request, context)

    def remove_listener_from_state(self, nqn, gw_name, trtype, traddr, port, context):
        if not context:
            return pb2.req_status(status=0, error_message=os.strerror(0))

        if context:
            assert self.omap_lock.locked()
        # Update gateway state
        try:
            self.gateway_state.remove_listener(nqn, gw_name, trtype, traddr, port)
        except Exception as ex:
            errmsg = f"Error persisting deletion of listener {traddr}:{port} from {nqn}:\n{ex}"
            self.logger.error(errmsg)
            return pb2.req_status(status=errno.EINVAL, error_message=errmsg)
        return pb2.req_status(status=0, error_message=os.strerror(0))

    def delete_listener_safe(self, request, context):
        """Deletes a listener from a subsystem at a given IP/Port."""

        ret = True
        traddr = GatewayConfig.escape_address_if_ipv6(request.traddr)
        delete_listener_error_prefix = f"Failure deleting listener {traddr}:{request.trsvcid} from {request.nqn}"

        trtype = GatewayEnumUtils.get_key_from_value(pb2.TransportType, request.trtype)
        if trtype == None:
            errmsg=f"{delete_listener_error_prefix}: Unknown transport type {request.trtype}"
            self.logger.error(errmsg)
            return pb2.req_status(status=errno.ENOKEY, error_message=errmsg)

        adrfam = GatewayEnumUtils.get_key_from_value(pb2.AddressFamily, request.adrfam)
        if adrfam == None:
            errmsg=f"{delete_listener_error_prefix}: Unknown address family {request.adrfam}"
            self.logger.error(errmsg)
            return pb2.req_status(status=errno.ENOKEY, error_message=errmsg)

        self.logger.info(f"Received request to delete {request.gateway_name}"
                         f" {trtype} listener for {request.nqn} at"
                         f" {traddr}:{request.trsvcid}, context: {context}")

        if self.is_discovery_nqn(request.nqn):
            errmsg=f"{delete_listener_error_prefix}: Can't delete a listener from a discovery subsystem"
            self.logger.error(errmsg)
            return pb2.req_status(status=errno.EINVAL, error_message=errmsg)

        with self.omap_lock(context=context):
            try:
                if request.gateway_name == self.gateway_name:
                    ret = rpc_nvmf.nvmf_subsystem_remove_listener(
                        self.spdk_rpc_client,
                        nqn=request.nqn,
                        trtype=trtype,
                        traddr=request.traddr,
                        trsvcid=str(request.trsvcid),
                        adrfam=adrfam,
                    )
                    self.logger.info(f"delete_listener: {ret}")
                else:
                    errmsg=f"{delete_listener_error_prefix}: Gateway name must match current gateway ({self.gateway_name})"
                    self.logger.error(f"{errmsg}")
                    return pb2.req_status(status=errno.ENOENT, error_message=errmsg)
            except Exception as ex:
                errmsg = f"{delete_listener_error_prefix}:\n{ex}"
                self.logger.error(errmsg)
                self.remove_listener_from_state(request.nqn, request.gateway_name, trtype,
                                                request.traddr, request.trsvcid, context)
                resp = self.parse_json_exeption(ex)
                status = errno.EINVAL
                if resp:
                    status = resp["code"]
                    errmsg = f"{delete_listener_error_prefix}: {resp['message']}"
                return pb2.req_status(status=status, error_message=errmsg)

            # Just in case SPDK failed with no exception
            if not ret:
                self.logger.error(delete_listener_error_prefix)
                self.remove_listener_from_state(request.nqn, request.gateway_name, trtype,
                                                request.traddr, request.trsvcid, context)
                return pb2.req_status(status=errno.EINVAL, error_message=delete_listener_error_prefix)

            return self.remove_listener_from_state(request.nqn, request.gateway_name, trtype,
                                                   request.traddr, request.trsvcid, context)

    def delete_listener(self, request, context=None):
        return self.execute_grpc_function(self.delete_listener_safe, request, context)

    def list_listeners_safe(self, request, context):
        """List listeners."""

        self.logger.info(f"Received request to list listeners for {request.subsystem}, context: {context}")
        try:
            ret = rpc_nvmf.nvmf_get_subsystems(self.spdk_rpc_client, nqn=request.subsystem)
            self.logger.info(f"list_listeners: {ret}")
        except Exception as ex:
            errmsg = f"Failure listing listeners, can't get subsystems:\n{ex}"
            self.logger.error(f"{errmsg}")
            resp = self.parse_json_exeption(ex)
            status = errno.EINVAL
            if resp:
                status = resp["code"]
                errmsg = f"Failure listing listeners, can't get subsystems: {resp['message']}"
            return pb2.listeners_info(status=status, error_message=errmsg, listeners=[])

        listeners = []
        for s in ret:
            try:
                if s["nqn"] != request.subsystem:
                    self.logger.warning(f'Got subsystem {s["nqn"]} instead of {request.subsystem}, ignore')
                    continue
                try:
                    listen_addrs = s["listen_addresses"]
                except Exception:
                    listen_addrs = []
                    pass
                for addr in listen_addrs:
                    one_listener = pb2.listener_info(gateway_name = self.gateway_name,
                                                     trtype = addr["trtype"].upper(),
                                                     adrfam = addr["adrfam"].lower(),
                                                     traddr = addr["traddr"],
                                                     trsvcid = int(addr["trsvcid"]))
                    listeners.append(one_listener)
                break
            except Exception:
                self.logger.exception(f"{s=} parse error: ")
                pass

        return pb2.listeners_info(status = 0, error_message = os.strerror(0), listeners=listeners)

    def list_listeners(self, request, context=None):
        return self.execute_grpc_function(self.list_listeners_safe, request, context)

    def list_subsystems_safe(self, request, context):
        """List subsystems."""

        ser_msg = ""
        if request.serial_number:
            ser_msg = f" with serial number {request.serial_number}"
        if request.subsystem_nqn:
            self.logger.info(f"Received request to list subsystem {request.subsystem_nqn}, context: {context}")
        else:
            self.logger.info(f"Received request to list subsystems{ser_msg}, context: {context}")

        subsystems = []
        try:
            if request.subsystem_nqn:
                ret = rpc_nvmf.nvmf_get_subsystems(self.spdk_rpc_client, nqn=request.subsystem_nqn)
            else:
                ret = rpc_nvmf.nvmf_get_subsystems(self.spdk_rpc_client)
            self.logger.info(f"list_subsystems: {ret}")
        except Exception as ex:
            errmsg = f"Failure listing subsystems:\n{ex}"
            self.logger.error(f"{errmsg}")
            resp = self.parse_json_exeption(ex)
            status = errno.ENODEV
            if resp:
                status = resp["code"]
                errmsg = f"Failure listing subsystems: {resp['message']}"
            return pb2.subsystems_info(status=status, error_message=errmsg, subsystems=[])

        for s in ret:
            try:
                if request.serial_number:
                    if s["serial_number"] != request.serial_number:
                        continue
                if s["subtype"] == "NVMe":
                    s["namespace_count"] = len(s["namespaces"])
                    s["enable_ha"] = self.get_subsystem_ha_status(s["nqn"])
                else:
                    s["namespace_count"] = 0
                    s["enable_ha"] = False
                # Parse the JSON dictionary into the protobuf message
                subsystem = pb2.subsystem()
                json_format.Parse(json.dumps(s), subsystem, ignore_unknown_fields=True)
                subsystems.append(subsystem)
            except Exception:
                self.logger.exception(f"{s=} parse error: ")
                pass

        return pb2.subsystems_info(status = 0, error_message = os.strerror(0), subsystems=subsystems)

    def list_subsystems(self, request, context=None):
        return self.execute_grpc_function(self.list_subsystems_safe, request, context)

    def get_spdk_nvmf_log_flags_and_level_safe(self, request, context):
        """Gets spdk nvmf log flags, log level and log print level"""
        self.logger.info(f"Received request to get SPDK nvmf log flags and level")
        log_flags = []
        with self.omap_lock(context=context):
            try:
                nvmf_log_flags = {key: value for key, value in rpc_log.log_get_flags(
                    self.spdk_rpc_client).items() if key.startswith('nvmf')}
                for flag, flagvalue in nvmf_log_flags.items():
                    pb2_log_flag = pb2.spdk_log_flag_info(name = flag, enabled = flagvalue)
                    log_flags.append(pb2_log_flag)
                spdk_log_level = rpc_log.log_get_level(self.spdk_rpc_client)
                spdk_log_print_level = rpc_log.log_get_print_level(self.spdk_rpc_client)
                self.logger.info(f"spdk log flags: {nvmf_log_flags}, " 
                                 f"spdk log level: {spdk_log_level}, "
                                 f"spdk log print level: {spdk_log_print_level}")
            except Exception as ex:
                errmsg = f"Failure getting SPDK log levels and nvmf log flags:\n{ex}"
                self.logger.error(f"{errmsg}")
                resp = self.parse_json_exeption(ex)
                status = errno.ENOKEY
                if resp:
                    status = resp["code"]
                    errmsg = f"Failure getting SPDK log levels and nvmf log flags: {resp['message']}"
                return pb2.spdk_nvmf_log_flags_and_level_info(status = status, error_message = errmsg)

        return pb2.spdk_nvmf_log_flags_and_level_info(
            nvmf_log_flags=log_flags,
            log_level = spdk_log_level,
            log_print_level = spdk_log_print_level,
            status = 0,
            error_message = os.strerror(0))

    def get_spdk_nvmf_log_flags_and_level(self, request, context=None):
        return self.execute_grpc_function(self.get_spdk_nvmf_log_flags_and_level_safe, request, context)

    def set_spdk_nvmf_logs_safe(self, request, context):
        """Enables spdk nvmf logs"""
        log_level = None
        print_level = None
        ret_log = False
        ret_print = False
        if request.log_level:
            log_level = GatewayEnumUtils.get_key_from_value(pb2.LogLevel, request.log_level)
            if log_level == None:
                errmsg=f"Unknown log level {request.log_level}"
                self.logger.error(f"{errmsg}")
                return pb2.req_status(status=errno.ENOKEY, error_message=errmsg)

        if request.print_level:
            print_level = GatewayEnumUtils.get_key_from_value(pb2.LogLevel, request.print_level)
            if print_level == None:
                errmsg=f"Unknown print level {request.print_level}"
                self.logger.error(f"{errmsg}")
                return pb2.req_status(status=errno.ENOKEY, error_message=errmsg)

        self.logger.info(f"Received request to set SPDK nvmf logs: log_level: {log_level}, print_level: {print_level}")

        with self.omap_lock(context=context):
            try:
                nvmf_log_flags = [key for key in rpc_log.log_get_flags(self.spdk_rpc_client).keys() \
                                  if key.startswith('nvmf')]
                ret = [rpc_log.log_set_flag(
                    self.spdk_rpc_client, flag=flag) for flag in nvmf_log_flags]
                self.logger.info(f"Set SPDK nvmf log flags {nvmf_log_flags} to TRUE: {ret}")
                if log_level:
                    ret_log = rpc_log.log_set_level(self.spdk_rpc_client, level=log_level)
                    self.logger.info(f"Set log level to {log_level}: {ret_log}")
                if print_level:
                    ret_print = rpc_log.log_set_print_level(
                        self.spdk_rpc_client, level=print_level)
                    self.logger.info(f"Set log print level to {print_level}: {ret_print}")
            except Exception as ex:
                errmsg="Failure setting SPDK log levels:\n{ex}"
                self.logger.error(f"{errmsg}")
                for flag in nvmf_log_flags:
                    rpc_log.log_clear_flag(self.spdk_rpc_client, flag=flag)
                resp = self.parse_json_exeption(ex)
                status = errno.EINVAL
                if resp:
                    status = resp["code"]
                    errmsg = f"Failure setting SPDK log levels: {resp['message']}"
                return pb2.req_status(status=status, error_message=errmsg)

        status = 0
        errmsg = os.strerror(0)
        if log_level and not ret_log:
            status = errno.EINVAL
            errmsg = "Failure setting SPDK log level"
        elif print_level and not ret_print:
            status = errno.EINVAL
            errmsg = "Failure setting SPDK print log level"
        elif not all(ret):
            status = errno.EINVAL
            errmsg = "Failure setting some SPDK nvmf log flags"
        return pb2.req_status(status=status, error_message=errmsg)

    def set_spdk_nvmf_logs(self, request, context=None):
        return self.execute_grpc_function(self.set_spdk_nvmf_logs_safe, request, context)

    def disable_spdk_nvmf_logs_safe(self, request, context):
        """Disables spdk nvmf logs"""
        self.logger.info(f"Received request to disable SPDK nvmf logs")

        with self.omap_lock(context=context):
            try:
                nvmf_log_flags = [key for key in rpc_log.log_get_flags(self.spdk_rpc_client).keys() \
                                  if key.startswith('nvmf')]
                ret = [rpc_log.log_clear_flag(self.spdk_rpc_client, flag=flag) for flag in nvmf_log_flags]
                logs_level = [rpc_log.log_set_level(self.spdk_rpc_client, level='NOTICE'),
                              rpc_log.log_set_print_level(self.spdk_rpc_client, level='INFO')]
                ret.extend(logs_level)
            except Exception as ex:
                self.logger.error(f"disable_spdk_nvmf_logs failed with:\n{ex}")
                errmsg = f"Failure in disable SPDK nvmf log flags\n{ex}"
                resp = self.parse_json_exeption(ex)
                status = errno.EINVAL
                if resp:
                    status = resp["code"]
                    errmsg = f"Failure in disable SPDK nvmf log flags: {resp['message']}"
                return pb2.req_status(status=status, error_message=errmsg)

        status = 0
        errmsg = os.strerror(0)
        if not all(ret):
            status = errno.EINVAL
            errmsg = "Failure in disable SPDK nvmf log flags"
        return pb2.req_status(status=status, error_message=errmsg)

    def disable_spdk_nvmf_logs(self, request, context=None):
        return self.execute_grpc_function(self.disable_spdk_nvmf_logs_safe, request, context)

    def parse_version(self, version):
        if not version:
            return None
        try:
            vlist = version.split(".")
            if len(vlist) != 3:
                raise Exception
            v1 = int(vlist[0])
            v2 = int(vlist[1])
            v3 = int(vlist[2])
        except Exception:
            self.logger.error(f"Can't parse version \"{version}\"")
            return None
        return (v1, v2, v3)

    def get_gateway_info_safe(self, request, context):
        """Get gateway's info"""

        self.logger.info(f"Received request to get gateway's info")
        gw_version_string = os.getenv("NVMEOF_VERSION")
        cli_version_string = request.cli_version
        addr = self.config.get_with_default("gateway", "addr", "")
        port = self.config.get_with_default("gateway", "port", "")
        ret = pb2.gateway_info(cli_version = request.cli_version,
                               version = gw_version_string,
                               name = self.gateway_name,
                               group = self.gateway_group,
                               addr = addr,
                               port = port,
                               bool_status = True,
                               status = 0,
                               error_message = os.strerror(0))
        cli_ver = self.parse_version(cli_version_string)
        gw_ver = self.parse_version(gw_version_string)
        if cli_ver != None and gw_ver != None and cli_ver < gw_ver:
            ret.bool_status = False
            ret.status = errno.EINVAL
            ret.error_message = f"CLI version {cli_version_string} is older than gateway's version {gw_version_string}"
        elif not gw_version_string:
            ret.bool_status = False
            ret.status = errno.ENOKEY
            ret.error_message = "Gateway's version not found"
        elif not gw_ver:
            ret.bool_status = False
            ret.status = errno.EINVAL
            ret.error_message = f"Invalid gateway's version {gw_version_string}"
        if not cli_version_string:
            self.logger.warning(f"No CLI version specified, can't check version compatibility")
        elif not cli_ver:
            self.logger.warning(f"Invalid CLI version {cli_version_string}, can't check version compatibility")
        if ret.status == 0:
            log_func = self.logger.info
        else:
            log_func = self.logger.error
        log_func(f"Gateway's info:\n{ret}")
        return ret

    def get_gateway_info(self, request, context=None):
        """Get gateway's info"""
        return self.execute_grpc_function(self.get_gateway_info_safe, request, context)
