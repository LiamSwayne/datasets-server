# SPDX-License-Identifier: Apache-2.0
# Copyright 2022 The HuggingFace Authors.

import logging
from http import HTTPStatus
from typing import Any, Literal, Mapping, Optional

from libcommon.constants import PROCESSING_STEP_SPLIT_OPT_IN_OUT_URLS_COUNT_VERSION
from libcommon.simple_cache import SplitFullName

from worker.job_runner import (
    CompleteJobResult,
    JobRunnerError,
    get_previous_step_or_raise,
)
from worker.job_runners.split.split_job_runner import SplitJobRunner
from worker.utils import OptInOutUrlsCountResponse

SplitOptInOutUrlsCountJobRunnerErrorCode = Literal["PreviousStepFormatError"]


class SplitOptInOutUrlsCountJobRunnerError(JobRunnerError):
    """Base class for exceptions in this module."""

    def __init__(
        self,
        message: str,
        status_code: HTTPStatus,
        code: SplitOptInOutUrlsCountJobRunnerErrorCode,
        cause: Optional[BaseException] = None,
        disclose_cause: bool = False,
    ):
        super().__init__(
            message=message, status_code=status_code, code=code, cause=cause, disclose_cause=disclose_cause
        )


class PreviousStepFormatError(SplitOptInOutUrlsCountJobRunnerError):
    """Raised when the content of the previous step has not the expected format."""

    def __init__(self, message: str, cause: Optional[BaseException] = None):
        super().__init__(message, HTTPStatus.INTERNAL_SERVER_ERROR, "PreviousStepFormatError", cause, False)


def compute_opt_in_out_urls_count_response(
    dataset: str,
    config: str,
    split: str,
) -> OptInOutUrlsCountResponse:
    logging.info(f"get opt-in-out-urls-count for dataset={dataset} config={config} split={split}")

    opt_in_out_urls_scan = get_previous_step_or_raise(
        kinds=["split-opt-in-out-urls-scan"], dataset=dataset, config=config, split=split
    )

    try:
        content = opt_in_out_urls_scan.response["content"]
        opt_in_out_urls_count = OptInOutUrlsCountResponse(
            has_urls_columns=content["has_urls_columns"],
            num_opt_in_urls=content["num_opt_in_urls"],
            num_opt_out_urls=content["num_opt_out_urls"],
            num_scanned_rows=content["num_scanned_rows"],
            num_urls=content["num_urls"],
            urls_columns=content["urls_columns"],
        )
    except KeyError as e:
        raise PreviousStepFormatError("Previous step did not return the expected content.", e) from e

    return opt_in_out_urls_count


class SplitOptInOutUrlsCountJobRunner(SplitJobRunner):
    @staticmethod
    def get_job_type() -> str:
        return "split-opt-in-out-urls-count"

    @staticmethod
    def get_job_runner_version() -> int:
        return PROCESSING_STEP_SPLIT_OPT_IN_OUT_URLS_COUNT_VERSION

    def compute(self) -> CompleteJobResult:
        return CompleteJobResult(
            compute_opt_in_out_urls_count_response(
                dataset=self.dataset,
                config=self.config,
                split=self.split,
            )
        )

    def get_new_splits(self, _: Mapping[str, Any]) -> set[SplitFullName]:
        """Get the set of new splits, from the content created by compute."""
        if self.config is None or self.split is None:
            raise ValueError("config and split are required")
        return {SplitFullName(dataset=self.dataset, config=self.config, split=self.split)}
