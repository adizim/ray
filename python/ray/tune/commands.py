from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import logging
import os
import sys
import subprocess
import operator
from datetime import datetime

import pandas as pd
from pandas.api.types import is_string_dtype, is_numeric_dtype
from ray.tune.result import TRAINING_ITERATION, MEAN_ACCURACY, MEAN_LOSS
from ray.tune.trial import Trial
from ray.tune.analysis.experiment_analysis import ExperimentAnalysis
from ray.tune import TuneError
try:
    from tabulate import tabulate
except ImportError:
    tabulate = None

logger = logging.getLogger(__name__)

EDITOR = os.getenv("EDITOR", "vim")

TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S (%A)"

DEFAULT_EXPERIMENT_INFO_KEYS = (
    "trainable_name",
    "experiment_tag",
    "trial_id",
    "status",
    "last_update_time",
)

DEFAULT_RESULT_KEYS = (TRAINING_ITERATION, MEAN_ACCURACY, MEAN_LOSS)

DEFAULT_PROJECT_INFO_KEYS = (
    "name",
    "total_trials",
    "running_trials",
    "terminated_trials",
    "error_trials",
    "last_updated",
)

try:
    TERM_HEIGHT, TERM_WIDTH = subprocess.check_output(["stty", "size"]).split()
    TERM_HEIGHT, TERM_WIDTH = int(TERM_HEIGHT), int(TERM_WIDTH)
except subprocess.CalledProcessError:
    TERM_HEIGHT, TERM_WIDTH = 100, 100

OPERATORS = {
    '<': operator.lt,
    '<=': operator.le,
    '==': operator.eq,
    '!=': operator.ne,
    '>=': operator.ge,
    '>': operator.gt,
}


def _check_tabulate():
    """Checks whether tabulate is installed."""
    if tabulate is None:
        raise ImportError(
            "Tabulate not installed. Please run `pip install tabulate`.")


def print_format_output(dataframe):
    """Prints output of given dataframe to fit into terminal.

    Returns:
        table (pd.DataFrame): Final outputted dataframe.
        dropped_cols (list): Columns dropped due to terminal size.
        empty_cols (list): Empty columns (dropped on default).
    """
    print_df = pd.DataFrame()
    dropped_cols = []
    empty_cols = []
    # column display priority is based on the info_keys passed in
    for i, col in enumerate(dataframe):
        if dataframe[col].isnull().all():
            # Don't add col to print_df if is fully empty
            empty_cols += [col]
            continue

        print_df[col] = dataframe[col]
        test_table = tabulate(print_df, headers="keys", tablefmt="psql")
        if str(test_table).index('\n') > TERM_WIDTH:
            # Drop all columns beyond terminal width
            print_df.drop(col, axis=1, inplace=True)
            dropped_cols += list(dataframe.columns)[i:]
            break

    table = tabulate(
        print_df, headers="keys", tablefmt="psql", showindex="never")

    print(table)
    if dropped_cols:
        print("Dropped columns:", dropped_cols)
        print("Please increase your terminal size to view remaining columns.")
    if empty_cols:
        print("Empty columns:", empty_cols)

    return table, dropped_cols, empty_cols


def list_trials(experiment_path,
                sort=None,
                output=None,
                filter_op=None,
                info_keys=DEFAULT_EXPERIMENT_INFO_KEYS,
                result_keys=DEFAULT_RESULT_KEYS):
    """Lists trials in the directory subtree starting at the given path.

    Args:
        experiment_path (str): Directory where trials are located.
            Corresponds to Experiment.local_dir/Experiment.name.
        sort (str): Key to sort by.
        output (str): Name of file where output is saved.
        filter_op (str): Filter operation in the format
            "<column> <operator> <value>".
        info_keys (list): Keys that are displayed.
        result_keys (list): Keys of last result that are displayed.
    """
    _check_tabulate()

    try:
        checkpoints_df = ExperimentAnalysis(experiment_path).dataframe()
    except TuneError:
        print("No experiment state found!")
        sys.exit(0)

    result_keys = ["last_result:{}".format(k) for k in result_keys]
    col_keys = [
        k for k in list(info_keys) + result_keys if k in checkpoints_df
    ]
    checkpoints_df = checkpoints_df[col_keys]

    if "last_update_time" in checkpoints_df:
        with pd.option_context("mode.use_inf_as_null", True):
            datetime_series = checkpoints_df["last_update_time"].dropna()

        datetime_series = datetime_series.apply(
            lambda t: datetime.fromtimestamp(t).strftime(TIMESTAMP_FORMAT))
        checkpoints_df["last_update_time"] = datetime_series

    if "logdir" in checkpoints_df:
        # logdir often too verbose to view in table, so drop experiment_path
        checkpoints_df["logdir"] = checkpoints_df["logdir"].str.replace(
            experiment_path, '')

    if filter_op:
        col, op, val = filter_op.split(' ')
        col_type = checkpoints_df[col].dtype
        if is_numeric_dtype(col_type):
            val = float(val)
        elif is_string_dtype(col_type):
            val = str(val)
        # TODO(Andrew): add support for datetime and boolean
        else:
            raise ValueError("Unsupported dtype for '{}': {}".format(
                val, col_type))
        op = OPERATORS[op]
        filtered_index = op(checkpoints_df[col], val)
        checkpoints_df = checkpoints_df[filtered_index]

    if sort:
        if sort not in checkpoints_df:
            raise KeyError("Sort Index '{}' not in: {}".format(
                sort, list(checkpoints_df)))
        checkpoints_df = checkpoints_df.sort_values(by=sort)

    print_format_output(checkpoints_df)

    if output:
        file_extension = os.path.splitext(output)[1].lower()
        if file_extension in (".p", ".pkl", ".pickle"):
            checkpoints_df.to_pickle(output)
        elif file_extension == ".csv":
            checkpoints_df.to_csv(output, index=False)
        else:
            raise ValueError("Unsupported filetype: {}".format(output))
        print("Output saved at:", output)


def list_experiments(project_path,
                     sort=None,
                     output=None,
                     filter_op=None,
                     info_keys=DEFAULT_PROJECT_INFO_KEYS):
    """Lists experiments in the directory subtree.

    Args:
        project_path (str): Directory where experiments are located.
            Corresponds to Experiment.local_dir.
        sort (str): Key to sort by.
        output (str): Name of file where output is saved.
        filter_op (str): Filter operation in the format
            "<column> <operator> <value>".
        info_keys (list): Keys that are displayed.
    """
    _check_tabulate()
    base, experiment_folders, _ = next(os.walk(project_path))

    experiment_data_collection = []

    for experiment_dir in experiment_folders:
        analysis_obj, checkpoints_df = None, None
        try:
            analysis_obj = ExperimentAnalysis(
                os.path.join(project_path, experiment_dir))
            checkpoints_df = analysis_obj.dataframe()
        except TuneError:
            logger.debug("No experiment state found in %s", experiment_dir)
            continue

        # Format time-based values.
        stats = analysis_obj.stats()
        time_values = {
            "start_time": stats.get("_start_time"),
            "last_updated": stats.get("timestamp"),
        }

        formatted_time_values = {
            key: datetime.fromtimestamp(val).strftime(TIMESTAMP_FORMAT)
            if val else None
            for key, val in time_values.items()
        }

        experiment_data = {
            "name": experiment_dir,
            "total_trials": checkpoints_df.shape[0],
            "running_trials": (
                checkpoints_df["status"] == Trial.RUNNING).sum(),
            "terminated_trials": (
                checkpoints_df["status"] == Trial.TERMINATED).sum(),
            "error_trials": (checkpoints_df["status"] == Trial.ERROR).sum(),
        }
        experiment_data.update(formatted_time_values)
        experiment_data_collection.append(experiment_data)

    if not experiment_data_collection:
        print("No experiments found!")
        sys.exit(0)

    info_df = pd.DataFrame(experiment_data_collection)
    col_keys = [k for k in list(info_keys) if k in info_df]
    if not col_keys:
        print("None of keys {} in experiment data!".format(info_keys))
        sys.exit(0)
    info_df = info_df[col_keys]

    if filter_op:
        col, op, val = filter_op.split(' ')
        col_type = info_df[col].dtype
        if is_numeric_dtype(col_type):
            val = float(val)
        elif is_string_dtype(col_type):
            val = str(val)
        # TODO(Andrew): add support for datetime and boolean
        else:
            raise ValueError("Unsupported dtype for '{}': {}".format(
                val, col_type))
        op = OPERATORS[op]
        filtered_index = op(info_df[col], val)
        info_df = info_df[filtered_index]

    if sort:
        if sort not in info_df:
            raise KeyError("Sort Index '{}' not in: {}".format(
                sort, list(info_df)))
        info_df = info_df.sort_values(by=sort)

    print_format_output(info_df)

    if output:
        file_extension = os.path.splitext(output)[1].lower()
        if file_extension in (".p", ".pkl", ".pickle"):
            info_df.to_pickle(output)
        elif file_extension == ".csv":
            info_df.to_csv(output, index=False)
        else:
            raise ValueError("Unsupported filetype: {}".format(output))
        print("Output saved at:", output)


def add_note(path, filename="note.txt"):
    """Opens a txt file at the given path where user can add and save notes.

    Args:
        path (str): Directory where note will be saved.
        filename (str): Name of note. Defaults to "note.txt"
    """
    path = os.path.expanduser(path)
    assert os.path.isdir(path), "{} is not a valid directory.".format(path)

    filepath = os.path.join(path, filename)
    exists = os.path.isfile(filepath)

    try:
        subprocess.call([EDITOR, filepath])
    except Exception as exc:
        logger.error("Editing note failed!")
        raise exc
    if exists:
        print("Note updated at:", filepath)
    else:
        print("Note created at:", filepath)
