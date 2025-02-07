# Copyright (c) Meta Platforms, Inc. and affiliates.
# All rights reserved.
#
# This source code is licensed under the BSD-style license found in the
# LICENSE file in the root directory of this source tree.

import os
import pickle
import unittest
import uuid
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Dict, List, Sequence, Set

import torch
import torch.distributed.launcher as pet

from torcheval.metrics import Metric
from torcheval.metrics.toolkit import clone_metric, sync_and_compute
from torchtnt.utils import copy_data_to_device, init_from_env
from typing_extensions import Literal

BATCH_SIZE = 16
# By default, we can test merge_state() on 4 processes with
# each processes will update states twice, which is 8 updates in total.
NUM_TOTAL_UPDATES = 8
NUM_PROCESSES = 4


@dataclass
class _MetricClassTestCaseSpecs:
    metric: Metric
    state_names: Set[str]
    update_kwargs: Dict[str, Any]
    # pyre-ignore[4]: There's no restrictions on return types of a specific metric computation
    compute_result: Any
    # pyre-ignore[4]: There's no restrictions on return types of a specific metric computation
    merge_and_compute_result: Any
    num_total_updates: int = NUM_TOTAL_UPDATES
    num_processes: int = NUM_PROCESSES
    atol: float = 1e-8
    rtol: float = 1e-5
    device: Literal["cuda", "cpu"] = "cpu"


class MetricClassTester(unittest.TestCase):
    def setUp(self) -> None:
        self._test_case_spec = None

    def run_class_implementation_tests(
        self,
        metric: Metric,
        state_names: Set[str],
        update_kwargs: Dict[str, Any],
        # pyre-ignore[2]: There's no restrictions on return types of a specific metric computation
        compute_result: Any,
        # pyre-ignore[2]: There's no restrictions on return types of a specific metric computation
        merge_and_compute_result: Any = None,
        num_total_updates: int = NUM_TOTAL_UPDATES,
        num_processes: int = NUM_PROCESSES,
        atol: float = 1e-8,
        rtol: float = 1e-5,
    ) -> None:
        """
        Run a test case to verify metric class implementations.

        Args:
            metric: The metric object to test against.
            state_names: Set of names of metric state variables.
            update_kwargs: Key value pairs representing the arguments of
                ``metric.update()``. For each argument value, len(val)
                should be equal to ``num_total_updates``, which should be
                greater than 2 to test the behaviour in distributed training.
                Make sure the argument value of ``i``th ``update()`` call
                could be accessed by ``updated_kwargs[arg_name][i]``.
                Usually ``input`` and ``target`` can be generated by
                ``torch.rand(num_updates, BATCH_SIZE, ..) ``.
            compute_result: The expected return value of ``Metric.compute()``
                after metric is updated number of batches times with arguments
                in update_kwargs.
            num_total_updates: Number of updates in the update_kwargs.
            num_processes: Number of processes for metric computation distributed
                training. ``num_total_updates`` should be divisible by
                ``num_processes``.
            atol: Absolute tolerance used in ``torch.testing.assert_close``
            rtol: Relative tolerance used in ``torch.testing.assert_close``
        """
        # update args and state names should not be empty
        self.assertTrue(update_kwargs)
        self.assertTrue(state_names)
        self.assertTrue(
            all(
                len(arg_val) == num_total_updates for arg_val in update_kwargs.values()
            ),
            "The outer size of each update argument should be equal to number of updates",
        )
        self.assertGreater(num_total_updates, 1)
        self.assertGreater(num_processes, 1)
        self.assertEqual(num_total_updates % num_processes, 0)

        merge_and_compute_result = (
            compute_result
            if merge_and_compute_result is None
            else merge_and_compute_result
        )
        self._test_case_spec = _MetricClassTestCaseSpecs(
            metric,
            state_names,
            update_kwargs,
            compute_result,
            merge_and_compute_result,
            num_total_updates,
            num_processes,
            atol,
            rtol,
        )

        test_devices = ("cpu", "cuda") if torch.cuda.is_available() else ("cpu",)
        for device in test_devices:
            self._test_case_spec.device = device
            self._test_case_spec = copy_data_to_device(
                self._test_case_spec, torch.device(self._test_case_spec.device)
            )
            self._test_init()
            self._test_update_and_compute()
            self._test_merge_state()
            # testing on GPU might cause CUDA oom
            if device == "cpu":
                self._test_sync_and_compute()

    def _test_metric_pickable_hashable(self, metric: Metric) -> None:
        pickled_metric = pickle.dumps(metric)
        loaded_metric = pickle.loads(pickled_metric)
        self.assert_state_unchanged(
            self._test_case_spec.state_names, loaded_metric, metric
        )
        self.assertTrue(hash(metric))

    def _test_state_dict_load_state_dict(self, metric: Metric) -> None:
        test_metric = deepcopy(metric).reset()
        test_metric.load_state_dict(metric.state_dict())
        self.assert_state_unchanged(
            self._test_case_spec.state_names, test_metric, metric
        )

    def _test_init(self) -> None:
        metric = self._test_case_spec.metric
        self.assertEqual(
            set(metric._state_name_to_default.keys()), self._test_case_spec.state_names
        )
        self._test_metric_pickable_hashable(metric)
        self._test_state_dict_load_state_dict(metric)

    def _test_update_and_compute(self) -> None:
        result = None
        test_metric = deepcopy(self._test_case_spec.metric)
        for i in range(self._test_case_spec.num_total_updates):
            # test chainable call
            current_batch_update_kwargs = {
                k: v[i] for k, v in self._test_case_spec.update_kwargs.items()
            }
            result = test_metric.update(**current_batch_update_kwargs).compute()

        final_computation_result = test_metric.compute()
        # compute result from single process should be same as one merged from multiple processes
        assert_result_close(
            final_computation_result,
            self._test_case_spec.compute_result,
            atol=self._test_case_spec.atol,
            rtol=self._test_case_spec.rtol,
        )
        # compute should be idempotent
        assert_result_close(final_computation_result, result)
        self._test_metric_pickable_hashable(test_metric)
        self._test_state_dict_load_state_dict(test_metric)

    def _test_merge_state(self) -> None:
        num_processes = self._test_case_spec.num_processes
        num_total_updates = self._test_case_spec.num_total_updates
        state_names = self._test_case_spec.state_names
        test_metrics: List[Metric] = [
            deepcopy(self._test_case_spec.metric) for i in range(num_processes)
        ]

        # no errors when merge before update, compute result should be the same
        # compared to merge_state is not called
        test_metric_0_copy = deepcopy(test_metrics[0])
        result_before_merge = test_metric_0_copy.update(
            **{k: v[0] for k, v in self._test_case_spec.update_kwargs.items()}
        ).compute()
        test_metrics_copy = deepcopy(test_metrics)
        test_metrics_copy[0].merge_state(test_metrics_copy[1:])
        result_after_merge = (
            test_metrics_copy[0]
            .update(**{k: v[0] for k, v in self._test_case_spec.update_kwargs.items()})
            .compute()
        )
        assert_result_close(result_before_merge, result_after_merge)

        # call merge_state before update
        # update metric 0 and then metric 0 merges metric 1
        test_metric_0_copy = deepcopy(test_metrics[0])
        test_metric_1_copy = deepcopy(test_metrics[1])
        test_metric_0_copy.update(
            **{k: v[0] for k, v in self._test_case_spec.update_kwargs.items()}
        )
        test_metric_0_copy.merge_state([test_metric_1_copy])
        assert_result_close(result_before_merge, test_metric_0_copy.compute())

        # update metric 1 and then metric 0 merges metric 1
        test_metric_0_copy = deepcopy(test_metrics[0])
        test_metric_1_copy = deepcopy(test_metrics[1])
        test_metric_1_copy.update(
            **{k: v[0] for k, v in self._test_case_spec.update_kwargs.items()}
        )
        test_metric_0_copy.merge_state([test_metric_1_copy])
        assert_result_close(result_before_merge, test_metric_0_copy.compute())

        # update, merge, compute
        for i in range(num_processes):
            for j in range(num_total_updates // num_processes):
                metric_i_current_batch_update_kwargs = {
                    k: v[i * num_total_updates // num_processes + j]
                    for k, v in self._test_case_spec.update_kwargs.items()
                }
                test_metrics[i].update(**metric_i_current_batch_update_kwargs).compute()
        test_metrics_unmerged = [deepcopy(metric) for metric in test_metrics]
        final_computation_result = (
            test_metrics[0].merge_state(test_metrics[1:]).compute()
        )
        assert_result_close(
            final_computation_result,
            self._test_case_spec.merge_and_compute_result,
            atol=self._test_case_spec.atol,
            rtol=self._test_case_spec.rtol,
        )

        # input metric states unchanged
        for i in range(1, num_processes):
            self.assert_state_unchanged(
                state_names, test_metrics_unmerged[i], test_metrics[i]
            )

        # compute should be idempotent
        torch.testing.assert_close(
            final_computation_result,
            test_metrics[0].compute(),
            equal_nan=True,
        )
        self._test_metric_pickable_hashable(test_metrics[0])
        self._test_state_dict_load_state_dict(test_metrics[0])

        # metric can still be updated and computed after merged
        test_metrics[0].update(
            **{k: v[0] for k, v in self._test_case_spec.update_kwargs.items()}
        ).compute()

        # merge metrics on different devices
        if torch.cuda.is_available():
            test_metrics_copy = [deepcopy(metric) for metric in test_metrics]
            past_device_type = self._test_case_spec.device
            new_device_type = "cuda" if self._test_case_spec.device == "cpu" else "cuda"
            self.assertEqual(test_metrics_copy[0]._device.type, past_device_type)
            test_metrics_copy[0].to(new_device_type).merge_state(test_metrics_copy[1:])
            for i in range(1, num_processes):
                self.assert_state_unchanged(
                    state_names, test_metrics_copy[i], test_metrics[i]
                )
                self.assertEqual(test_metrics_copy[i]._device.type, past_device_type)
            self.assertEqual(test_metrics_copy[0]._device.type, new_device_type)

    def _test_sync_and_compute(self) -> None:
        lc = pet.LaunchConfig(
            min_nodes=1,
            max_nodes=1,
            nproc_per_node=self._test_case_spec.num_processes,
            run_id=str(uuid.uuid4()),
            rdzv_backend="c10d",
            rdzv_endpoint="localhost:0",
            max_restarts=0,
            monitor_interval=1,
        )
        pet.elastic_launch(lc, entrypoint=self._test_per_process_sync_and_compute)(
            self._test_case_spec,
        )

    @staticmethod
    def _test_per_process_sync_and_compute(
        test_spec: _MetricClassTestCaseSpecs,
    ) -> None:
        init_from_env(device_type="cpu")
        rank = int(os.environ["RANK"])

        metric = clone_metric(test_spec.metric)
        num_total_updates = test_spec.num_total_updates
        num_processes = test_spec.num_processes

        for i in range(num_total_updates // num_processes):
            metric_current_batch_update_kwargs = {
                k: v[rank * num_total_updates // num_processes + i]
                for k, v in test_spec.update_kwargs.items()
            }
            metric.update(**metric_current_batch_update_kwargs).compute()
        final_computation_result = sync_and_compute(metric)
        if rank == 0:
            assert_result_close(
                final_computation_result,
                test_spec.merge_and_compute_result,
                atol=test_spec.atol,
                rtol=test_spec.rtol,
            )

    def assert_state_unchanged(
        self, state_names: Set[str], metric1: Metric, metric2: Metric
    ) -> None:
        for state in state_names:
            assert_result_close(
                getattr(metric1, state),
                getattr(metric2, state),
            )


def assert_result_close(
    # pyre-ignore[2]: There's no restrictions on return types of a specific metric computation
    result: Any,
    # pyre-ignore[2]: There's no restrictions on return types of a specific metric computation
    expected_result: Any,
    atol: float = 1e-8,
    rtol: float = 1e-5,
) -> None:
    tc = unittest.TestCase()
    tc.assertEqual(type(result), type(expected_result))
    if isinstance(result, torch.Tensor):
        tc.assertTrue(isinstance(expected_result, torch.Tensor))
        torch.testing.assert_close(
            result, expected_result, atol=atol, rtol=rtol, equal_nan=True
        )
    elif isinstance(result, Sequence):
        tc.assertTrue(isinstance(expected_result, Sequence))
        tc.assertEqual(len(result), len(expected_result))
        for element, expected_element in zip(result, expected_result):
            assert_result_close(element, expected_element, atol, rtol)
    else:
        # add more supported type to result comparision if needed
        raise ValueError("Compute result comparision is not supported.")
