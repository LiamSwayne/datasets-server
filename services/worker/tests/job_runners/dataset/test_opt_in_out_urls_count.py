# SPDX-License-Identifier: Apache-2.0
# Copyright 2022 The HuggingFace Authors.

from http import HTTPStatus
from typing import Any, Callable, List

import pytest
from libcommon.processing_graph import ProcessingStep
from libcommon.queue import Priority
from libcommon.resources import CacheMongoResource, QueueMongoResource
from libcommon.simple_cache import upsert_response

from worker.config import AppConfig
from worker.job_runner import PreviousStepError
from worker.job_runners.dataset.opt_in_out_urls_count import (
    DatasetOptInOutUrlsCountJobRunner,
)


@pytest.fixture(autouse=True)
def prepare_and_clean_mongo(app_config: AppConfig) -> None:
    # prepare the database before each test, and clean it afterwards
    pass


GetJobRunner = Callable[[str, AppConfig, bool], DatasetOptInOutUrlsCountJobRunner]


@pytest.fixture
def get_job_runner(
    cache_mongo_resource: CacheMongoResource,
    queue_mongo_resource: QueueMongoResource,
) -> GetJobRunner:
    def _get_job_runner(
        dataset: str,
        app_config: AppConfig,
        force: bool = False,
    ) -> DatasetOptInOutUrlsCountJobRunner:
        return DatasetOptInOutUrlsCountJobRunner(
            job_info={
                "type": DatasetOptInOutUrlsCountJobRunner.get_job_type(),
                "dataset": dataset,
                "config": None,
                "split": None,
                "job_id": "job_id",
                "force": force,
                "priority": Priority.NORMAL,
            },
            common_config=app_config.common,
            worker_config=app_config.worker,
            processing_step=ProcessingStep(
                name=DatasetOptInOutUrlsCountJobRunner.get_job_type(),
                input_type="config",
                requires=[],
                required_by_dataset_viewer=False,
                ancestors=[],
                children=[],
                parents=[],
                job_runner_version=DatasetOptInOutUrlsCountJobRunner.get_job_runner_version(),
            ),
        )

    return _get_job_runner


@pytest.mark.parametrize(
    "dataset,config_names_status,config_names_content,config_upstream_status"
    + ",config_upstream_content,expected_error_code,expected_content,should_raise",
    [
        (
            "dataset_ok",
            HTTPStatus.OK,
            {
                "config_names": [
                    {"dataset": "dataset_ok", "config": "config1"},
                    {"dataset": "dataset_ok", "config": "config2"},
                ]
            },
            [HTTPStatus.OK, HTTPStatus.OK],
            [
                {
                    "urls_columns": ["image_url", "url"],
                    "num_opt_in_urls": 10,
                    "num_opt_out_urls": 20,
                    "num_urls": 100,
                    "num_scanned_rows": 100,
                    "has_urls_columns": True,
                },
                {
                    "urls_columns": ["image_url", "label", "url"],
                    "num_opt_in_urls": 10,
                    "num_opt_out_urls": 0,
                    "num_urls": 50,
                    "num_scanned_rows": 300,
                    "has_urls_columns": True,
                },
            ],
            None,
            {
                "urls_columns": ["image_url", "label", "url"],
                "num_opt_in_urls": 20,
                "num_opt_out_urls": 20,
                "num_urls": 150,
                "num_scanned_rows": 400,
                "has_urls_columns": True,
            },
            False,
        ),
        (
            "previos_step_error",
            HTTPStatus.INTERNAL_SERVER_ERROR,
            {},
            [],
            [],
            "PreviousStepError",
            None,
            True,
        ),
        (
            "previous_step_format_error",
            HTTPStatus.OK,
            {
                "config_names": [
                    {"dataset": "dataset_ok", "config": "config1"},
                    {"dataset": "dataset_ok", "config": "config2"},
                ]
            },
            [HTTPStatus.OK],
            [{"wrong_format": None}],
            "PreviousStepFormatError",
            None,
            True,
        ),
    ],
)
def test_compute(
    app_config: AppConfig,
    get_job_runner: GetJobRunner,
    dataset: str,
    config_names_status: HTTPStatus,
    config_names_content: Any,
    config_upstream_status: List[HTTPStatus],
    config_upstream_content: List[Any],
    expected_error_code: str,
    expected_content: Any,
    should_raise: bool,
) -> None:
    upsert_response(
        kind="/config-names",
        dataset=dataset,
        content=config_names_content,
        http_status=config_names_status,
    )

    if config_names_status == HTTPStatus.OK:
        for split_item, status, content in zip(
            config_names_content["config_names"], config_upstream_status, config_upstream_content
        ):
            upsert_response(
                kind="config-opt-in-out-urls-count",
                dataset=dataset,
                config=split_item["config"],
                content=content,
                http_status=status,
            )

    job_runner = get_job_runner(dataset, app_config, False)
    if should_raise:
        with pytest.raises(Exception) as e:
            job_runner.compute()
        assert e.type.__name__ == expected_error_code
    else:
        assert job_runner.compute().content == expected_content


def test_doesnotexist(app_config: AppConfig, get_job_runner: GetJobRunner) -> None:
    dataset = "doesnotexist"
    job_runner = get_job_runner(dataset, app_config, False)
    with pytest.raises(PreviousStepError):
        job_runner.compute()