# SPDX-License-Identifier: Apache-2.0
# Copyright 2022 The HuggingFace Authors.

from http import HTTPStatus

from libcommon.metrics import CacheTotalMetric, JobTotalMetric
from libcommon.processing_graph import ProcessingGraph
from libcommon.queue import Queue
from libcommon.simple_cache import upsert_response

from cache_maintenance.metrics import collect_metrics


def test_collect_metrics() -> None:
    dataset = "test_dataset"
    config = None
    split = None
    content = {"some": "content"}

    step_name = "test_type"
    processing_graph = ProcessingGraph(
        processing_graph_specification={step_name: {"input_type": "dataset", "job_runner_version": 1}}
    )
    queue = Queue()
    queue.upsert_job(
        job_type=processing_graph.get_step(step_name).job_type, dataset="dataset", config="config", split="split"
    )
    upsert_response(
        kind=processing_graph.get_step(step_name).cache_kind,
        dataset=dataset,
        config=config,
        split=split,
        content=content,
        http_status=HTTPStatus.OK,
    )

    collect_metrics(processing_graph=processing_graph)

    cache_metrics = CacheTotalMetric.objects()
    assert cache_metrics
    assert len(cache_metrics) == 1

    job_metrics = JobTotalMetric.objects()
    assert job_metrics
    assert len(job_metrics) == 6  # One by each job state, see libcommon.queue.get_jobs_count_by_status
    waiting_job = next((job for job in job_metrics if job.status == "waiting"), None)
    assert waiting_job
    assert waiting_job.total == 1

    remaining_status = [job for job in job_metrics if job.status != "waiting"]
    assert remaining_status
    assert all(job.total == 0 for job in remaining_status)