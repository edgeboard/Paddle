#   Copyright (c) 2019 PaddlePaddle Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Optimizer Factory."""

__all__ = ["DistributedAdam"]
import paddle.fluid as fluid
from paddle.fluid.distribute_lookup_table import find_distributed_lookup_table
from paddle.fluid.distribute_lookup_table import find_distributed_lookup_table_inputs
from paddle.fluid.distribute_lookup_table import find_distributed_lookup_table_outputs
from google.protobuf import text_format
from collections import OrderedDict
from .node import DownpourWorker, DownpourServer
from . import ps_pb2 as pslib


class DistributedOptimizerImplBase(object):
    """
    DistributedOptimizerImplBase
    base class of optimizers
    """

    def __init__(self, optimizer):
        self._optimizer = optimizer
        self._learning_rate = optimizer._learning_rate
        self._regularization = optimizer.regularization

    def minimize(self,
                 losses,
                 startup_program=None,
                 parameter_list=None,
                 no_grad_set=None):
        """
        Args:
            losses(Variable): loss variable defined by user
            startup_program(Program): startup program that defined by user
            parameter_list(str list): parameter names defined by users
            no_grad_set(set): a set of variables that is defined by users
                so that these variables do not need gradient computation
        """
        pass


class DistributedAdam(DistributedOptimizerImplBase):
    """
    DistributedAdam
    adam optimizer in distributed training
    """

    def __init__(self, optimizer):
        # todo(guru4elephant): add more optimizers here as argument
        # todo(guru4elephant): make learning_rate as a variable
        super(DistributedAdam, self).__init__(optimizer)
        self._window = 1
        self.type = "downpour"
        self.data_norm_name = [
            ".batch_size", ".batch_square_sum", ".batch_sum",
            ".batch_size@GRAD", ".batch_square_sum@GRAD", ".batch_sum@GRAD"
        ]

    def _find_distributed_lookup_table_inputs(self, program, table_names):
        """
        Find input variable of distribute lookup table in program.
        We could support multi-distribute table now.
        Args:
            program(Program): given program, locate distributed lookup table
            table_name(str): given table names that is found beforehand
        Returns:
            inputs
        """
        local_vars = program.current_block().vars
        inputs_dict = dict()
        for table_name in table_names:
            inputs_dict[table_name] = []

        for op in program.global_block().ops:
            if op.type == "lookup_table":
                if op.input("W")[0] in table_names:
                    inputs_dict[op.input("W")[0]].extend(
                        [local_vars[name] for name in op.input("Ids")])
        return inputs_dict

    def _find_distributed_lookup_table_outputs(self, program, table_names):
        """
        Find output variable of distribute lookup table in program.
        We could support multi-distribute table now.
        Args:
            programs(Program): given program, locate distributed lookup table
            table_name(str): given table name that is found beforehand
        Returns:
            outputs
        """
        local_vars = program.current_block().vars
        outputs_dict = dict()
        for table_name in table_names:
            outputs_dict[table_name] = []

        for op in program.global_block().ops:
            if op.type == "lookup_table":
                if op.input("W")[0] in table_names:
                    outputs_dict[op.input("W")[0]].extend(
                        [local_vars[name] for name in op.output("Out")])
        return outputs_dict

    def _find_distributed_lookup_table_grads(self, program, table_names):
        local_vars = program.current_block().vars
        grads_dict = dict()
        for table_name in table_names:
            grads_dict[table_name] = []

        for op in program.global_block().ops:
            if op.type == "lookup_table_grad" and op.input("W")[
                    0] in table_names:
                grads_dict[op.input("W")[0]].extend(
                    [local_vars[name] for name in op.input("Out@GRAD")])
        return grads_dict

    def _find_multi_distributed_lookup_table(self, losses):
        """
        find multi-sparse-table
        """
        table_names = set()
        cnt = 0
        tmp_list = []
        ret_list = []
        for loss in losses:
            for op in loss.block.program.global_block().ops:
                if op.type == "lookup_table":
                    if op.attr('is_distributed') is True:
                        table_name = op.input("W")[0]
                        if table_name not in table_names:
                            table_names.add(table_name)
                            tmp_list.append([table_name, cnt])
                            cnt += 1
        tmp_list.sort(key=lambda k: k[1])
        for x in tmp_list:
            ret_list.append(x[0])
        return ret_list

    def _minimize(self,
                  losses,
                  startup_program=None,
                  parameter_list=None,
                  no_grad_set=None,
                  strategy={}):
        """
        DownpounSGD is a distributed optimizer so
        that user can call minimize to generate backward
        operators and optimization operators within minmize function
        Args:
            loss(Variable): loss variable defined by user
            startup_program(Program): startup program that defined by user
            parameter_list(str list): parameter names defined by users
            no_grad_set(set): a set of variables that is defined by users
            so that these variables do not need gradient computation
            strategy(dict): user-defined properties
        Returns:
            [optimize_ops, grads_and_weights]
        """
        # sparse table names of each program
        prog_id_to_sparse_table = OrderedDict()
        # inputs_dict and outputs_dict of sparse tables of each program
        prog_id_to_inputs_dict = OrderedDict()
        prog_id_to_outputs_dict = OrderedDict()
        # related to PSParameter
        ps_param = pslib.PSParameter()
        # related to ServerParameter
        server = DownpourServer()
        # program to worker (related to DownpourTrainerParameter)
        prog_id_to_worker = OrderedDict()
        # param_grads of each program
        prog_id_to_param_grads = OrderedDict()
        # sparse_grads of each program
        prog_id_to_sparse_grads = OrderedDict()

        sparse_table_to_index = OrderedDict()
        sparse_table_index = 0
        for loss in losses:
            sparse_table = self._find_multi_distributed_lookup_table([loss])
            prog_id = str(id(loss.block.program))
            prog_id_to_sparse_table[prog_id] = sparse_table

            # get sparse_table_to_index
            for tn in sparse_table:
                if sparse_table_to_index.get(tn) is None:
                    sparse_table_to_index[tn] = sparse_table_index
                    sparse_table_index += 1

            # get inputs_dict
            inputs_dict = self._find_distributed_lookup_table_inputs(
                loss.block.program, sparse_table)
            prog_id_to_inputs_dict[prog_id] = inputs_dict
            # get outputs_dict
            outputs_dict = self._find_distributed_lookup_table_outputs(
                loss.block.program, sparse_table)
            prog_id_to_outputs_dict[prog_id] = outputs_dict

            prog_id_to_worker[prog_id] = DownpourWorker(self._window)

            # param_grads of program
            params_grads = sorted(
                fluid.backward.append_backward(loss, parameter_list,
                                               no_grad_set),
                key=lambda x: x[0].name)
            prog_id_to_param_grads[prog_id] = params_grads

            grads_dict = self._find_distributed_lookup_table_grads(
                loss.block.program, sparse_table)
            prog_id_to_sparse_grads[prog_id] = grads_dict

        # if user specify a fleet_desc.prototxt file, then load the file
        # instead of creating default fleet_desc.prototxt.
        # user can specify server_param or trainer_param or fs_client_param.
        if strategy.get("fleet_desc_file") is not None:
            fleet_desc_file = strategy["fleet_desc_file"]
            with open(fleet_desc_file) as f:
                text_format.Merge(f.read(), ps_param)
            server.get_desc().CopyFrom(ps_param.server_param)
            if len(ps_param.trainer_param) == 1:
                for k in prog_id_to_worker:
                    prog_id_to_worker[k].get_desc().CopyFrom(
                        ps_param.trainer_param[0])
            else:
                if len(ps_param.trainer_param) != len(prog_id_to_worker):
                    raise ValueError(
                        "trainer param size != program size, %s vs %s" %
                        (len(ps_param.trainer_param), len(prog_id_to_worker)))
                idx = 0
                # prog_id_to_worker is OrderedDict
                for k in prog_id_to_worker:
                    prog_id_to_worker[k].get_desc().CopyFrom(
                        ps_param.trainer_param[idx])
                    idx += 1

        # ServerParameter add all sparse tables
        for tn in sparse_table_to_index:
            sparse_table_index = sparse_table_to_index[tn]
            if strategy.get(tn) is not None:
                server.add_sparse_table(sparse_table_index, strategy[tn])
            else:
                server.add_sparse_table(sparse_table_index, None)

        # each DownpourTrainerParameter add its own sparse tables
        for loss in losses:
            prog_id = str(id(loss.block.program))
            worker = prog_id_to_worker[prog_id]
            inputs_dict = prog_id_to_inputs_dict[prog_id]
            outputs_dict = prog_id_to_outputs_dict[prog_id]
            for tn in prog_id_to_sparse_table[prog_id]:
                sparse_table_index = sparse_table_to_index[tn]
                grads_dict = prog_id_to_sparse_grads[prog_id]
                worker.add_sparse_table(sparse_table_index, inputs_dict[tn],
                                        outputs_dict[tn], grads_dict[tn])

        dense_start_table_id = len(sparse_table_to_index)
        dense_table_index = len(sparse_table_to_index)
        program_configs = {}
        # ServerParameter add all dense tables
        # each DownpourTrainerParameter add its own dense tables
        for loss_index in range(len(losses)):
            program_id = str(id(losses[loss_index].block.program))
            worker = prog_id_to_worker[program_id]
            sparse_table_names = prog_id_to_sparse_table[program_id]
            sparse_table_index = \
                [sparse_table_to_index[i] for i in sparse_table_names]

            program_configs[program_id] = {
                "pull_sparse": [t_index for t_index in sparse_table_index],
                "push_sparse": [t_index for t_index in sparse_table_index]
            }

            params_grads = prog_id_to_param_grads[program_id]
            params = []
            grads = []
            data_norm_params = []
            data_norm_grads = []
            for i in params_grads:
                is_data_norm_data = False
                for data_norm_name in self.data_norm_name:
                    if i[0].name.endswith(data_norm_name):
                        is_data_norm_data = True
                        data_norm_params.append(i[0])
                if not is_data_norm_data:
                    params.append(i[0])

            for i in params_grads:
                is_data_norm_data = False
                for data_norm_grad in self.data_norm_name:
                    if i[0].name.endswith(data_norm_grad):
                        is_data_norm_data = True
                        data_norm_grads.append(i[1])
                if not is_data_norm_data:
                    grads.append(i[1])

            if strategy.get('dense_table') is not None:
                server.add_dense_table(dense_table_index, params, grads,
                                       strategy['dense_table'],
                                       sparse_table_names)
            else:
                server.add_dense_table(dense_table_index, params, grads, None,
                                       sparse_table_names)
            worker.add_dense_table(dense_table_index, self._learning_rate,
                                   params, grads, dense_start_table_id,
                                   sparse_table_names)
            program_configs[program_id]["pull_dense"] = [dense_table_index]
            program_configs[program_id]["push_dense"] = [dense_table_index]
            if len(data_norm_params) != 0 and len(data_norm_grads) != 0:
                dense_table_index += 1
                if strategy.get('datanorm_table') is not None:
                    server.add_data_norm_table(
                        dense_table_index, self._learning_rate,
                        data_norm_params, data_norm_grads,
                        strategy['datanorm_table'], sparse_table_names)
                else:
                    server.add_data_norm_table(
                        dense_table_index, self._learning_rate,
                        data_norm_params, data_norm_grads, None,
                        sparse_table_names)

                worker.add_dense_table(dense_table_index, self._learning_rate,
                                       data_norm_params, data_norm_grads,
                                       dense_start_table_id, sparse_table_names)
                program_configs[program_id]["pull_dense"].extend(
                    [dense_table_index])
                program_configs[program_id]["push_dense"].extend(
                    [dense_table_index])
            dense_table_index += 1

            # Todo(guru4elephant): figure out how to support more sparse parameters
            # currently only support lookup_table
            worker_skipped_ops = ["lookup_table", "lookup_table_grad"]
            if len(worker.get_desc().skip_op) == 0:
                worker.get_desc().skip_op.extend(worker_skipped_ops)

        ps_param.server_param.CopyFrom(server.get_desc())
        # prog_id_to_worker is OrderedDict
        if len(ps_param.trainer_param) == 0:
            for k in prog_id_to_worker:
                tp = ps_param.trainer_param.add()
                tp.CopyFrom(prog_id_to_worker[k].get_desc())

        opt_info = {}
        opt_info["program_id_to_worker"] = prog_id_to_worker
        opt_info["program_configs"] = program_configs
        opt_info["trainer"] = "DistMultiTrainer"
        opt_info["device_worker"] = "DownpourSGD"
        opt_info["optimizer"] = "DownpourSGD"
        opt_info["fleet_desc"] = ps_param
        opt_info["worker_skipped_ops"] = worker_skipped_ops
        opt_info["use_cvm"] = strategy.get("use_cvm", False)
        opt_info["stat_var_names"] = strategy.get("stat_var_names", [])
        opt_info["scale_datanorm"] = strategy.get("scale_datanorm", -1)
        opt_info["check_nan_var_names"] = strategy.get("check_nan_var_names",
                                                       [])
        opt_info["dump_slot"] = False
        opt_info["dump_converter"] = ""
        opt_info["dump_fields"] = strategy.get("dump_fields", [])
        opt_info["dump_file_num"] = strategy.get("dump_file_num", 16)
        opt_info["dump_fields_path"] = strategy.get("dump_fields_path", "")
        opt_info["dump_param"] = strategy.get("dump_param", [])
        if server._server.downpour_server_param.downpour_table_param[
                0].accessor.accessor_class == "DownpourCtrAccessor":
            opt_info["dump_slot"] = True
        opt_info["adjust_ins_weight"] = strategy.get("adjust_ins_weight", {})

        for loss in losses:
            loss.block.program._fleet_opt = opt_info

        param_grads_list = []
        for loss in losses:
            prog_id = str(id(loss.block.program))
            param_grads_list.append(prog_id_to_param_grads[prog_id])
        return None, param_grads_list, opt_info
