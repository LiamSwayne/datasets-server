# SPDX-License-Identifier: Apache-2.0
# Copyright 2023 The HuggingFace Authors.

import logging
from functools import partial
from pathlib import Path
from typing import List, Optional, Set
from urllib.parse import quote

import duckdb
from datasets import Features
from huggingface_hub._commit_api import (
    CommitOperation,
    CommitOperationAdd,
    CommitOperationDelete,
)
from huggingface_hub.hf_api import HfApi, RepoFile
from huggingface_hub.utils._errors import RepositoryNotFoundError
from libcommon.config import DuckDbIndexConfig
from libcommon.constants import PROCESSING_STEP_SPLIT_DUCKDB_INDEX_VERSION
from libcommon.exceptions import (
    DatasetNotFoundError,
    FileSystemError,
    NoIndexableColumnsError,
    ParquetResponseEmptyError,
    PreviousStepFormatError,
    SplitNotFoundError,
    UnsupportedIndexableColumnsError,
)
from libcommon.processing_graph import ProcessingStep
from libcommon.storage import StrPath, remove_dir
from libcommon.utils import JobInfo
from libcommon.viewer_utils.index_utils import create_index_dir_split
from pyarrow.parquet import ParquetFile
from tqdm.contrib.concurrent import thread_map

from worker.config import AppConfig
from worker.job_runners.split.split_job_runner import SplitJobRunner
from worker.utils import (
    CompleteJobResult,
    IndexRowsResponse,
    get_hf_fs,
    get_hf_parquet_uris,
    get_previous_step_or_raise,
)

DATASET_TYPE = "dataset"
STRING_FEATURE_DTYPE = "string"
VALUE_FEATURE_TYPE = "Value"
DUCKDB_DEFAULT_INDEX_FILENAME = "index.db"
UNSUPPORTED_FEATURES_MAGIC_STRINGS = ["'binary'", "Audio", "Image"]
CREATE_SEQUENCE_COMMAND = "CREATE OR REPLACE SEQUENCE serial START 1;"
CREATE_INDEX_COMMAND = "PRAGMA create_fts_index('data', '__id', '*', overwrite=1);"
CREATE_TABLE_COMMAND = "CREATE OR REPLACE TABLE data AS SELECT nextval('serial') AS __id, * FROM"
INSTALL_EXTENSION_COMMAND = "INSTALL '{extension}';"
LOAD_EXTENSION_COMMAND = "LOAD '{extension}';"
# TODO: What if __id field already exist?


def hf_hub_url(repo_id: str, filename: str, hf_endpoint: str, revision: str, url_template: str) -> str:
    return (hf_endpoint + url_template) % (repo_id, quote(revision, safe=""), filename)


def create_index_item(
    repo_file: RepoFile,
    dataset: str,
    config: str,
    split: str,
    hf_endpoint: str,
    target_revision: str,
    url_template: str,
) -> IndexRowsResponse:
    if repo_file.size is None:
        raise ValueError(f"Cannot get size of {repo_file.rfilename}")
    return {
        "dataset": dataset,
        "config": config,
        "split": split,
        "url": hf_hub_url(
            repo_id=dataset,
            filename=repo_file.rfilename,
            hf_endpoint=hf_endpoint,
            revision=target_revision,
            url_template=url_template,
        ),
        "filename": Path(repo_file.rfilename).name,
        "size": repo_file.size,
    }


def compute_index_rows(
    dataset: str,
    config: str,
    split: str,
    duckdb_index_directory: StrPath,
    target_revision: str,
    hf_endpoint: str,
    commit_message: str,
    url_template: str,
    hf_token: Optional[str],
    committer_hf_token: Optional[str],
) -> IndexRowsResponse:
    logging.info(f"get index-rows for dataset={dataset} config={config} split={split}")

    # validate split
    split_names_best_response = get_previous_step_or_raise(
        kinds=["config-split-names-from-streaming", "config-split-names-from-info"], dataset=dataset, config=config
    )
    try:
        splits_content = split_names_best_response.response["content"]["splits"]
    except Exception as e:
        raise PreviousStepFormatError("Previous step did not return the expected content.", e) from e

    if split not in [split_item["split"] for split_item in splits_content]:
        raise SplitNotFoundError(f"The split '{split}' does not exist for the config '{config}' of the dataset.")

    # get parquet content
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

    # look for string columns using the first rows
    string_columns = [column for column, feature in features.items() if STRING_FEATURE_DTYPE in str(feature)]

    if not string_columns:
        raise NoIndexableColumnsError("No string columns available to index.")

    # look for image, audio and binary columns, if present, raise exeception do not supported yet and index everything
    if any(
        feature
        for feature in features.values()
        if next(
            (feature_type for feature_type in UNSUPPORTED_FEATURES_MAGIC_STRINGS if feature_type in str(feature)), None
        )
        is not None
    ):
        raise UnsupportedIndexableColumnsError("Unsupported feature types for indexing.")

    try:
        parquet_urls = [content["url"] for content in parquet_files_content if content["split"] == split]

        if not parquet_urls:
            raise ParquetResponseEmptyError("No parquet files found.")
    except Exception as e:
        raise PreviousStepFormatError("Previous step did not return the expected content.") from e

    # create duckdb index location
    split_path, dir_path = create_index_dir_split(
        dataset=dataset, config=config, split=split, index_directory=duckdb_index_directory
    )
    db_location = dir_path / DUCKDB_DEFAULT_INDEX_FILENAME

    # configure duckdb extensions
    duckdb.execute(INSTALL_EXTENSION_COMMAND.format(extension="httpfs"))
    duckdb.execute(LOAD_EXTENSION_COMMAND.format(extension="httpfs"))
    duckdb.execute(INSTALL_EXTENSION_COMMAND.format(extension="fts"))
    duckdb.execute(LOAD_EXTENSION_COMMAND.format(extension="fts"))

    # index
    con = duckdb.connect(str(db_location))
    con.sql(CREATE_SEQUENCE_COMMAND)
    con.sql(f"{CREATE_TABLE_COMMAND} read_parquet({parquet_urls});")

    # TODO: by default, 'porter' stemmer is being used, use a specific one by dataset language in the future
    # see https://duckdb.org/docs/extensions/full_text_search.html for more deails about 'stemmer' parameter
    con.sql(CREATE_INDEX_COMMAND)

    # create the target revision if it does not exist yet (clone from initial commit to avoid cloning all repo's files)
    hf_api = HfApi(endpoint=hf_endpoint, token=hf_token)
    committer_hf_api = HfApi(endpoint=hf_endpoint, token=committer_hf_token)

    try:
        refs = hf_api.list_repo_refs(repo_id=dataset, repo_type=DATASET_TYPE)
        if all(ref.ref != target_revision for ref in refs.converts):
            initial_commit = hf_api.list_repo_commits(repo_id=dataset, repo_type=DATASET_TYPE)[-1].commit_id
            committer_hf_api.create_branch(
                repo_id=dataset, branch=target_revision, repo_type=DATASET_TYPE, revision=initial_commit
            )
    except RepositoryNotFoundError as err:
        raise DatasetNotFoundError("The dataset does not exist on the Hub.") from err
    except Exception as e:
        # TODO: improve error handling
        logging.error(str(e))
    target_dataset_info = hf_api.dataset_info(repo_id=dataset, revision=target_revision, files_metadata=False)
    all_repo_files: Set[str] = {f.rfilename for f in target_dataset_info.siblings}
    previous_index = f"{config}/{split}-{DUCKDB_DEFAULT_INDEX_FILENAME}"
    delete_operations: List[CommitOperation] = []
    if previous_index in all_repo_files:
        delete_operations.append(CommitOperationDelete(path_in_repo=previous_index))
        logging.debug(f"{delete_operations=}")

    # send the files to the target revision
    add_operations: List[CommitOperation] = [
        CommitOperationAdd(
            path_in_repo=f"{config}/{split}-{DUCKDB_DEFAULT_INDEX_FILENAME}", path_or_fileobj=db_location
        )
    ]
    logging.debug(f"{add_operations=}")

    # TODO: Delete local index file
    committer_hf_api.create_commit(
        repo_id=dataset,
        repo_type=DATASET_TYPE,
        revision=target_revision,
        operations=delete_operations + add_operations,
        commit_message=commit_message,
        parent_commit=target_dataset_info.sha,
    )

    # call the API again to get the list of parquet files
    target_dataset_info = hf_api.dataset_info(repo_id=dataset, revision=target_revision, files_metadata=True)
    repo_files = [
        repo_file
        for repo_file in target_dataset_info.siblings
        if repo_file.rfilename.startswith(f"{config}/{split}") and repo_file.rfilename.endswith(".db")
    ]
    if len(repo_files) != 1:
        # TODO: improve exception type
        raise Exception("NO FILE WAS UPLOADED TO BRANCH")
    index_file = repo_files[0]

    remove_dir(dir_path)
    return create_index_item(
        repo_file=index_file,
        dataset=dataset,
        config=config,
        split=split,
        hf_endpoint=hf_endpoint,
        target_revision=target_revision,
        url_template=url_template,
    )


class SplitDuckDbIndexJobRunner(SplitJobRunner):
    duckdb_index_config: DuckDbIndexConfig
    duckdb_index_directory: StrPath

    def __init__(
        self,
        job_info: JobInfo,
        app_config: AppConfig,
        processing_step: ProcessingStep,
        duckdb_index_directory: StrPath,
    ) -> None:
        super().__init__(
            job_info=job_info,
            app_config=app_config,
            processing_step=processing_step,
        )
        self.duckdb_index_directory = duckdb_index_directory
        self.duckdb_index_config = app_config.duckdb_index

    @staticmethod
    def get_job_type() -> str:
        return "split-duckdb-index"

    @staticmethod
    def get_job_runner_version() -> int:
        return PROCESSING_STEP_SPLIT_DUCKDB_INDEX_VERSION

    def compute(self) -> CompleteJobResult:
        return CompleteJobResult(
            compute_index_rows(
                dataset=self.dataset,
                config=self.config,
                split=self.split,
                duckdb_index_directory=self.duckdb_index_directory,
                hf_token=self.app_config.common.hf_token,
                url_template=self.duckdb_index_config.url_template,
                commit_message=self.duckdb_index_config.commit_message,
                committer_hf_token=self.duckdb_index_config.committer_hf_token,
                hf_endpoint=self.app_config.common.hf_endpoint,
                target_revision=self.duckdb_index_config.target_revision,
            )
        )
