"""
Script for training a model on a set of data
Should be called form within a U-Time project directory, call 'ut init' first.
All hyperparameters should be stored in the hparams.yaml file (generated by
ut init).
"""

import logging
import numpy as np
import os
import tables
import tensorflow as tf
from argparse import ArgumentParser
from utime import Defaults
from utime.train import Trainer
from utime.models.model_init import (init_model,
                                     load_from_file,
                                     prepare_for_continued_training)
from utime.hyperparameters import YAMLHParams
from utime.utils.system import find_and_set_gpus
from utime.utils.scriptutils import assert_project_folder, add_logging_file_handler
from utime.utils.scriptutils.train import (get_train_and_val_datasets,
                                           get_h5_train_and_val_datasets,
                                           get_generators,
                                           get_samples_per_epoch,
                                           save_final_weights,
                                           remove_previous_session,
                                           get_all_dataset_hparams,
                                           init_default_project_structure)
from psg_utils.dataset.queue.utils import get_data_queues

logger = logging.getLogger(__name__)


def get_argparser():
    """
    Returns an argument parser for this script
    """
    parser = ArgumentParser(description='Fit a U-Time model defined in'
                                        ' a project folder. Invoke '
                                        '"ut init" to start a new project.')
    parser.add_argument("--num_gpus", type=int, default=1,
                        help="Number of GPUs to use for this job (default=1)")
    parser.add_argument("--force_gpus", type=str, default="")
    parser.add_argument("--continue_training", action="store_true",
                        help="Continue the last training session")
    parser.add_argument("--initialize_from", type=str, default=None,
                        help="Path to a model weights file to initialize from.")
    parser.add_argument("--overwrite", action='store_true',
                        help='Overwrite previous training session in the project path including all models and logs.')
    parser.add_argument("--log_file", type=str, default="training_log",
                        help="Relative path (from Defaults.LOG_DIR as specified by ut --log_dir flag) of "
                             "output log file for this script. "
                             "Set to an empty string to not save any logs to file for this run. "
                             "Default is 'training_log'")
    parser.add_argument("--datasets", nargs="*", default=None,
                        help="Optional space-sepparated list of dataset IDs as specified in hyperparameters file to "
                             "consider for training and validation (unless --no_val flag is set). Ignores all other "
                             "datasets specified in the file (e.g., datasets to use only for testing).")
    parser.add_argument("--just", type=int, default=None,
                        help="For testing purposes, run only on the first "
                             "[just] training and validation samples.")
    parser.add_argument("--no_val", action="store_true",
                        help="For testing purposes, do not perform validation.")
    parser.add_argument("--max_train_samples_per_epoch", type=int,
                        default=5e5,
                        help="Maximum number of sleep stages to sample in each"
                             "epoch. (defaults to 5e5)")
    parser.add_argument("--n_epochs", type=int, default=None,
                        help="Overwrite the number of epochs specified in the"
                             " hyperparameter file with this number (int).")
    parser.add_argument("--channels", nargs='*', type=str, default=None,
                        help="A list of channels to use instead of those "
                             "specified in the parameter file.")
    parser.add_argument("--train_queue_type", type=str, default='eager',
                        help="Data queueing type for training data. One of:"
                             " 'eager', 'lazy', 'limitation'. The 'eager' "
                             "queue loads all data into memory up front, the "
                             "'lazy' queue loads data only when needed and the"
                             " 'limitation' queue maintains a buffer of "
                             "loaded data limited according to "
                             "--max_loaded_per_dataset and "
                             "--num_access_before_reload which is recycled "
                             "continuously. Note: with --preprocessed, the "
                             "'eager' queue is always used, as data is not "
                             "loaded from the HDF5 archive even with calls to"
                             " .load() methods.")
    parser.add_argument("--val_queue_type", type=str, default='lazy',
                        help="Queue type for validation data. "
                             "See --train_queue_type.")
    parser.add_argument("--max_loaded_per_dataset", type=int, default=40,
                        help="Set a number of maximum SleepStudy objects to"
                             " be kept loaded in memory at any given time per "
                             "dataset. OBS: If training on multiple datasets,"
                             " this means that 'max_loaded_per_dataset' times"
                             " the number of datasets will be the number of"
                             "studies loaded at a given time.")
    parser.add_argument("--num_access_before_reload", type=int, default=32,
                        help="If --max_loaded_per_dataset is set, this value "
                             "determines how many times a SleepStudy object "
                             "is accessed (normally for extracting data) "
                             "before the study is unloaded and replaced by "
                             "another random study from the same dataset.")
    parser.add_argument("--preprocessed", action='store_true',
                        help="Run on a pre-processed dataset as output by the "
                             "'ut preprocess' script. Streams data from disk "
                             "which significantly reduces memory load. "
                             "However, running with --preprocessed ignores "
                             "settings such as 'strip_func', "
                             "'quality_control_function', 'scaler' etc. "
                             "(those were applied in the preprocess step). "
                             "Changes to any of those parameters requires a "
                             "rerun of 'ut preprocess' to be effective with "
                             "the 'ut train' --preprocessed flag.")
    parser.add_argument("--final_weights_file_name", type=str,
                        default="model_weights.h5")
    parser.add_argument("--train_on_val", action="store_true",
                        help="Include the validation set in the training set."
                             " Will force --no_val to be active.")
    return parser


def assert_args(args):
    """ Implements a limited set of checks on the passed arguments """
    if args.continue_training and args.initialize_from:
        raise ValueError("Should not specify both --continue_training and "
                         "--initialize_from")
    if args.max_train_samples_per_epoch < 1:
        raise ValueError("max_train_samples_per_epoch and ")
    if args.n_epochs is not None and args.n_epochs < 1:
        raise ValueError("n_epochs must be larger than >= 1.")


def update_hparams_with_command_line_arguments(hparams, args):
    """
    Overwrite hyperparameters stored in YAMLHparams object 'hparams' according
    to passed args.

    Args:
        hparams: (YAMLHparams) The hyperparameter object to write parameters to
        args:    (Namespace)   Passed command-line arguments
    """
    if isinstance(args.n_epochs, int) and args.n_epochs > 0:
        hparams.set_group("/fit/n_epochs",
                          value=args.n_epochs,
                          overwrite=True)
        hparams["fit"]["n_epochs"] = args.n_epochs
    if args.channels is not None and args.channels:
        # Channel selection hyperparameter might be stored in separate conf.
        # files. Here, we load them, set the channel value, and save them again
        for _, dataset_hparams in get_all_dataset_hparams(hparams).items():
            dataset_hparams.set_group("select_channels",
                                      value=args.channels,
                                      overwrite=True)
            dataset_hparams.delete_group('channel_sampling_groups', non_existing_ok=True)
            dataset_hparams.save_current()
    hparams.save_current()


def keep_n_random(*datasets, keep):
    """
    TODO

    Args:
        datasets:

    Returns:

    """
    logger.info(f"Keeping only {keep} random study in each dataset (--just)...")
    for dataset in datasets:
        dataset._pairs = np.random.choice(dataset.pairs, keep, replace=False)
        dataset.update_id_to_study_dict()


def run(args):
    """
    Run the script according to args - Please refer to the argparser.

    args:
        args:    (Namespace)  command-line arguments
    """
    assert_args(args)
    project_dir = os.path.abspath("./")
    model_dir_is_empty = assert_project_folder(project_dir)
    if args.overwrite and not args.continue_training:
        remove_previous_session(project_dir)
        init_default_project_structure(project_dir)
    elif not model_dir_is_empty and not args.continue_training:
        raise OSError("There seems to be files in the 'model' folder of this project directory, "
                      "but neither --overwrite nor --continue_training flags were set.")

    add_logging_file_handler(args.log_file, args.overwrite, mode="w" if not args.continue_training else "a")
    logger.info(f"Args dump: {vars(args)}")

    # Settings depending on --preprocessed flag.
    if args.preprocessed:
        yaml_path = Defaults.get_pre_processed_hparams_path(project_dir)
        dataset_func = get_h5_train_and_val_datasets
        train_queue_type = 'eager'
        val_queue_type = 'eager'
    else:
        yaml_path = Defaults.get_hparams_path(project_dir)
        dataset_func = get_train_and_val_datasets
        train_queue_type = args.train_queue_type
        val_queue_type = args.val_queue_type

    # Load hparams
    hparams = YAMLHParams(yaml_path)
    update_hparams_with_command_line_arguments(hparams, args)

    # Initialize and load (potentially multiple) datasets
    train_datasets, val_datasets = dataset_func(hparams, args.no_val, args.train_on_val, args.datasets)

    if args.just:
        keep_n_random(*train_datasets, *val_datasets, keep=args.just)

    # Get a data loader queue object for each dataset
    train_datasets_queues = get_data_queues(
        datasets=train_datasets,
        queue_type=train_queue_type,
        max_loaded_per_dataset=args.max_loaded_per_dataset,
        num_access_before_reload=args.num_access_before_reload
    )
    if val_datasets:
        val_dataset_queues = get_data_queues(
            datasets=val_datasets,
            queue_type=val_queue_type,
            max_loaded_per_dataset=args.max_loaded_per_dataset,
            num_access_before_reload=args.num_access_before_reload,
            study_loader=getattr(train_datasets_queues[0], 'study_loader', None)
        )
    else:
        val_dataset_queues = None

    # Get sequence generators for all datasets
    train_seq, val_seq = get_generators(train_datasets_queues,
                                        val_dataset_queues=val_dataset_queues,
                                        hparams=hparams)

    # Add additional (inferred) parameters to parameter file
    hparams.set_group("/build/n_classes", value=train_seq.n_classes, overwrite=True)
    hparams.set_group("/build/batch_shape", value=train_seq.batch_shape, overwrite=True)
    hparams.save_current()

    if args.continue_training:
        # Prepare the project directory for continued training.
        # Please refer to the function docstring for details
        parameter_file = prepare_for_continued_training(hparams=hparams,
                                                        project_dir=project_dir)
    else:
        parameter_file = args.initialize_from  # most often is None

    # Set the GPU visibility
    num_gpus = find_and_set_gpus(args.num_gpus, args.force_gpus)
    gpus = tf.config.list_physical_devices('GPU')
    assert len(gpus) == num_gpus, "Unexpected difference in number of visible and requested GPUs."
    # Initialize and potential load parameters into the model
    strategy = tf.distribute.MirroredStrategy(gpus) if gpus else tf.distribute.OneDeviceStrategy('/device:CPU:0')
    logger.info(f"Using TF distribution strategy: {strategy} on GPUs: {gpus}. (CPU:0 if empty).")
    with strategy.scope():
        model = init_model(hparams["build"], clear_previous=False)
        if parameter_file:
            load_from_file(model, parameter_file, by_name=True)

        # Prepare a trainer object and compile the model
        trainer = Trainer(model)
        trainer.compile_model(n_classes=hparams["build"].get("n_classes"),
                              reduction=tf.keras.losses.Reduction.NONE,
                              **hparams["fit"])

    # Fit the model on a number of samples as specified in args
    samples_pr_epoch = get_samples_per_epoch(train_seq, args.max_train_samples_per_epoch)

    _ = trainer.fit(train=train_seq,
                    val=val_seq,
                    train_samples_per_epoch=samples_pr_epoch,
                    **hparams["fit"])

    # Save weights to project_dir/model/{final_weights_file_name}.h5
    # Note: these weights are rarely used, as a checkpoint callback also saves
    # weights to this directory through training
    save_final_weights(project_dir,
                       model=model,
                       file_name=args.final_weights_file_name)


def entry_func(args=None):
    # Get the script to execute, parse only first input
    parser = get_argparser()
    args = parser.parse_args(args)
    try:
        run(args=args)
    finally:
        tables.file._open_files.close_all()


if __name__ == "__main__":
    entry_func()
