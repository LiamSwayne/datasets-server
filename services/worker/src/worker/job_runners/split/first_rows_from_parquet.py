# SPDX-License-Identifier: Apache-2.0
# Copyright 2022 The HuggingFace Authors.

import logging
from functools import lru_cache, partial
from typing import List, Optional

import pyarrow as pa
from datasets import Features
from huggingface_hub import HfFileSystem
from huggingface_hub.hf_file_system import safe_quote
from libcommon.constants import (
    PARQUET_REVISION,
    PROCESSING_STEP_SPLIT_FIRST_ROWS_FROM_PARQUET_VERSION,
    PROCESSING_STEP_SPLIT_FIRST_ROWS_FROM_STREAMING_VERSION,
)
from libcommon.exceptions import (
    FileSystemError,
    ParquetResponseEmptyError,
    PreviousStepFormatError,
    RowsPostProcessingError,
    TooBigContentError,
    TooManyColumnsError,
)
from libcommon.processing_graph import ProcessingStep
from libcommon.storage import StrPath
from libcommon.utils import JobInfo
from libcommon.viewer_utils.features import get_cell_value
from pyarrow.parquet import ParquetFile
from tqdm.contrib.concurrent import thread_map

from worker.config import AppConfig, FirstRowsConfig
from worker.job_runners.split.split_job_runner import SplitJobRunner
from worker.utils import (
    CompleteJobResult,
    JobRunnerInfo,
    Row,
    RowItem,
    SplitFirstRowsResponse,
    create_truncated_row_items,
    get_json_size,
    get_previous_step_or_raise,
    to_features_list,
)


def transform_rows(
    dataset: str,
    config: str,
    split: str,
    rows: List[RowItem],
    features: Features,
    assets_base_url: str,
    assets_directory: StrPath,
) -> List[Row]:
    return [
        {
            featureName: get_cell_value(
                dataset=dataset,
                config=config,
                split=split,
                row_idx=row_idx,
                cell=row["row"][featureName] if featureName in row["row"] else None,
                featureName=featureName,
                fieldType=fieldType,
                assets_base_url=assets_base_url,
                assets_directory=assets_directory,
            )
            for (featureName, fieldType) in features.items()
        }
        for row_idx, row in enumerate(rows)
    ]


@lru_cache(maxsize=128)
def get_hf_fs(hf_token: Optional[str]) -> HfFileSystem:
    """Get the Hugging Face filesystem.

    Args:
        hf_token (Optional[str]): The token to access the filesystem.
    Returns:
        HfFileSystem: The Hugging Face filesystem.
    """
    return HfFileSystem(token=hf_token)


def get_hf_parquet_uris(paths: List[str], dataset: str) -> List[str]:
    """Get the Hugging Face URIs from the Parquet branch of the dataset repository (see PARQUET_REVISION).

    Args:
        paths (List[str]): List of paths.
        dataset (str): The dataset name.
    Returns:
        List[str]: List of Parquet URIs.
    """
    return [f"hf://datasets/{dataset}@{safe_quote(PARQUET_REVISION)}/{path}" for path in paths]


def compute_first_rows_response(
    dataset: str,
    config: str,
    split: str,
    assets_base_url: str,
    hf_token: Optional[str],
    min_cell_bytes: int,
    rows_max_bytes: int,
    rows_max_number: int,
    rows_min_number: int,
    columns_max_number: int,
    assets_directory: StrPath,
) -> SplitFirstRowsResponse:
    logging.info(f"get first-rows for dataset={dataset} config={config} split={split}")

    # first ensure the tuple (dataset, config, split) exists on the Hub

    config_parquet_best_response = get_previous_step_or_raise(kinds=["config-parquet"], dataset=dataset, config=config)
    try:
        parquet_files_content = config_parquet_best_response.response["content"]["parquet_files"]
        sources = sorted(
            f"{config}/{parquet_file['filename']}"
            for parquet_file in parquet_files_content
            if parquet_file["split"] == split and parquet_file["config"] == config
        )
        if not sources:
            raise ParquetResponseEmptyError("No parquet files found.")
    except Exception as e:
        raise PreviousStepFormatError("Previous step did not return the expected content.") from e

    logging.debug(f"Found {len(sources)} parquet files for {dataset=}, {config=}, {split=}: {sources}")

    fs = get_hf_fs(hf_token=hf_token)
    source_uris = get_hf_parquet_uris(sources, dataset=dataset)
    desc = f"{dataset}/{config}/{split}"
    try:
        parquet_files: List[ParquetFile] = thread_map(
            partial(ParquetFile, filesystem=fs), source_uris, desc=desc, unit="pq", disable=True
        )
    except Exception as e:
        raise FileSystemError(f"Could not read the parquet files: {e}") from e

    # get the features
    features = Features.from_arrow_schema(parquet_files[0].schema.to_arrow_schema())

    if features and len(features) > columns_max_number:
        raise TooManyColumnsError(
            f"The number of columns ({len(features)}) exceeds the maximum supported number of columns"
            f" ({columns_max_number}). This is a current limitation of the datasets viewer. You can reduce the number"
            " of columns if you want the viewer to work."
        )

    # validate size of response without the rows
    features_list = to_features_list(features=features)
    response_features_only: SplitFirstRowsResponse = {
        "dataset": dataset,
        "config": config,
        "split": split,
        "features": features_list,
        "rows": [],
    }

    surrounding_json_size = get_json_size(response_features_only)
    if surrounding_json_size > rows_max_bytes:
        raise TooBigContentError(
            f"The size of the content of the first rows ({surrounding_json_size}) exceeds the maximum"
            f" supported size ({rows_max_bytes} B) even after truncation. Please report the issue."
        )

    # get the rows
    num_rows = 0
    last_row_group_id = 0
    row_group_readers = []
    for parquet_file in parquet_files:
        for group_id in range(parquet_file.metadata.num_row_groups):
            last_row_group_id = group_id
            row_group_readers.append(partial(parquet_file.read_row_group, i=group_id))
            if num_rows + parquet_file.metadata.row_group(group_id).num_rows >= rows_max_number:
                num_rows = rows_max_number
                break
            else:
                num_rows += parquet_file.metadata.row_group(group_id).num_rows
        else:
            continue
        break

    if len(row_group_readers) == 0:
        raise ParquetResponseEmptyError("No parquet files found.")

    pa_table = pa.concat_tables([row_group_readers[i]() for i in range(last_row_group_id + 1)])
    result = pa_table.slice(0, num_rows)

    rows = [
        RowItem(
            {
                "row_idx": idx,
                "row": row,
                "truncated_cells": [],
            }
        )
        for idx, row in enumerate(result.to_pylist())
    ]

    # transform the rows, if needed (e.g. save the images or audio to the assets, and return their URL)
    try:
        transformed_rows = transform_rows(
            dataset=dataset,
            config=config,
            split=split,
            rows=rows,
            features=features,
            assets_base_url=assets_base_url,
            assets_directory=assets_directory,
        )
    except Exception as err:
        raise RowsPostProcessingError(
            "Server error while post-processing the split rows. Please report the issue.",
            cause=err,
        ) from err

    # truncate the rows to fit within the restrictions, and prepare them as RowItems
    row_items = create_truncated_row_items(
        rows=transformed_rows,
        min_cell_bytes=min_cell_bytes,
        rows_max_bytes=rows_max_bytes - surrounding_json_size,
        rows_min_number=rows_min_number,
    )

    response = response_features_only
    response["rows"] = row_items
    return response


class SplitFirstRowsFromParquetJobRunner(SplitJobRunner):
    assets_directory: StrPath
    first_rows_config: FirstRowsConfig

    @staticmethod
    def get_job_type() -> str:
        return "split-first-rows-from-parquet"

    @staticmethod
    def get_job_runner_version() -> int:
        return PROCESSING_STEP_SPLIT_FIRST_ROWS_FROM_PARQUET_VERSION

    @staticmethod
    def get_parallel_job_runner() -> JobRunnerInfo:
        return JobRunnerInfo(
            job_runner_version=PROCESSING_STEP_SPLIT_FIRST_ROWS_FROM_STREAMING_VERSION,
            job_type="split-first-rows-from-streaming",
        )

    def __init__(
        self,
        job_info: JobInfo,
        app_config: AppConfig,
        processing_step: ProcessingStep,
        assets_directory: StrPath,
    ) -> None:
        super().__init__(
            job_info=job_info,
            app_config=app_config,
            processing_step=processing_step,
        )
        self.first_rows_config = app_config.first_rows
        self.assets_directory = assets_directory
        self.assets_base_url = app_config.assets.base_url

    def compute(self) -> CompleteJobResult:
        return CompleteJobResult(
            compute_first_rows_response(
                dataset=self.dataset,
                config=self.config,
                split=self.split,
                assets_base_url=self.assets_base_url,
                assets_directory=self.assets_directory,
                hf_token=self.app_config.common.hf_token,
                min_cell_bytes=self.first_rows_config.min_cell_bytes,
                rows_max_bytes=self.first_rows_config.max_bytes,
                rows_max_number=self.first_rows_config.max_number,
                rows_min_number=self.first_rows_config.min_number,
                columns_max_number=self.first_rows_config.columns_max_number,
            )
        )
