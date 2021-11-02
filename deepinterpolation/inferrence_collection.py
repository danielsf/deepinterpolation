import warnings

import h5py
import numpy as np
from deepinterpolation.generic import JsonLoader
from tensorflow.keras.models import load_model
import deepinterpolation.loss_collection as lc

import multiprocessing
from deepinterpolation.utils import _winnow_process_list

import tensorflow.config.threading
import tensorflow.compat.v1.logging
import os

import time

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'

tensorflow.config.threading.set_inter_op_parallelism_threads(1)
tensorflow.config.threading.set_intra_op_parallelism_threads(1)

tensorflow.compat.v1.logging.set_verbosity(
    tensorflow.compat.v1.logging.ERROR)


class fmri_inferrence:
    # This inferrence is specific to fMRI which is raster scanning for
    # denoising

    def __init__(self, inferrence_json_path, generator_obj):
        self.inferrence_json_path = inferrence_json_path
        self.generator_obj = generator_obj

        local_json_loader = JsonLoader(inferrence_json_path)
        local_json_loader.load_json()
        self.json_data = local_json_loader.json_data
        self.output_file = self.json_data["output_file"]
        self.model_path = self.json_data["model_path"]

        # This is used when output is a full volume to select only the center
        # currently only set to true. Future implementation could make smarter
        # scanning of the volume and leverage more
        # than just the center pixel
        if "single_voxel_output_single" in self.json_data.keys():
            self.single_voxel_output_single = self.json_data[
                "single_voxel_output_single"
            ]
        else:
            self.single_voxel_output_single = True

        self.model_path = self.json_data["model_path"]

        self.model = load_model(self.model_path)
        self.input_data_size = self.generator_obj.data_shape

    def run(self):
        chunk_size = list(self.generator_obj.data_shape)

        # Time is where we chunk the h5 file
        chunk_size[-1] = 1

        with h5py.File(self.output_file, "w") as file_handle:
            dset_out = file_handle.create_dataset(
                "data",
                shape=tuple(self.generator_obj.data_shape),
                chunks=tuple(chunk_size),
                dtype="float32",
            )
            # This was used to alter the volume infered and reduce computation
            # time
            # np.array([20])
            all_z_values = np.arange(0, self.input_data_size[2])
            all_y_values = np.arange(0, self.input_data_size[1])

            input_full = np.zeros(
                [
                    all_y_values.shape[0]
                    * all_z_values.shape[0]
                    * self.input_data_size[3],
                    self.generator_obj.pre_post_x * 2 + 1,
                    self.generator_obj.pre_post_y * 2 + 1,
                    self.generator_obj.pre_post_z * 2 + 1,
                    self.generator_obj.pre_post_t * 2 + 1,
                ],
                dtype="float32",
            )

            # We are looping across the volume
            for local_x in np.arange(0, self.input_data_size[0]):
                print("x=" + str(local_x))
                for index_y, local_y in enumerate(all_y_values):
                    print("y=" + str(local_y))
                    for index_z, local_z in enumerate(all_z_values):
                        for local_t in np.arange(0, self.input_data_size[3]):
                            (
                                input_full[
                                    local_t
                                    + index_z * self.input_data_size[3]
                                    + index_y
                                    * self.input_data_size[3]
                                    * all_z_values.shape[0],
                                    :,
                                    :,
                                    :,
                                    :,
                                ],
                                output_tmp,
                            ) = self.generator_obj.__data_generation__(
                                local_x, local_y, local_z, local_t
                            )

                predictions_data = self.model.predict(input_full)
                corrected_data = (
                    predictions_data * self.generator_obj.local_std
                    + self.generator_obj.local_mean
                )
                for index_y, local_y in enumerate(all_y_values):
                    for index_z, local_z in enumerate(all_z_values):
                        for local_t in np.arange(0, self.input_data_size[3]):
                            dset_out[
                                local_x, local_y, local_z, local_t
                            ] = corrected_data[
                                local_t
                                + index_z * self.input_data_size[3]
                                + index_y
                                * self.input_data_size[3]
                                * all_z_values.shape[0],
                                self.generator_obj.pre_post_x,
                                self.generator_obj.pre_post_y,
                                self.generator_obj.pre_post_z,
                                :,
                            ]


def load_model_worker(json_data):
    try:
        local_model_path = __get_local_model_path(json_data)
        model = __load_local_model(path=local_model_path)
    except KeyError:
        model = __load_model_from_mlflow(json_data)
    return model

def __get_local_model_path(json_data):
    try:
        model_path = json_data['model_path']
        warnings.warn('Loading model from model_path will be deprecated '
                      'in a future release')
    except KeyError:
        model_path = json_data['model_source']['local_path']
    return model_path

def __load_local_model(path: str):
    model = load_model(
        path,
        custom_objects={
            "annealed_loss": lc.loss_selector("annealed_loss")},
    )
    return model

def __load_model_from_mlflow(json_data):

    mlflow_registry_params = \
        json_data['model_source']['mlflow_registry']

    model_name = mlflow_registry_params['model_name']
    model_version = mlflow_registry_params.get('model_version')
    model_stage = mlflow_registry_params.get('model_stage')

    mlflow.set_tracking_uri(mlflow_registry_params['tracking_uri'])

    if model_version is not None:
        model_uri = f"models:/{model_name}/{model_version}"
    elif model_stage:
        model_uri = f"models:/{model_name}/{model_stage}"
    else:
        # Gets the latest version without any stage
        model_uri = f"models:/{model_name}/None"

    model = mlflow.keras.load_model(
        model_uri=model_uri
    )

    return model


def core_inference_worker(
        json_data,
        input_lookup,
        rescale,
        save_raw,
        output_dict,
        output_lock):

    model = load_model_worker(json_data)
    local_output = {}
    for dataset_index in input_lookup:
        local_lookup = input_lookup[dataset_index]
        local_data = local_lookup['local_data']
        local_mean = local_lookup['local_mean']
        local_std = local_lookup['local_std']

        predictions_data = model.predict(local_data[0])
        local_size = predictions_data.shape[0]

        if rescale:
            corrected_data = predictions_data * local_std + local_mean
        else:
            corrected_data = predictions_data

        if save_raw:
            if rescale:
                corrected_raw = local_data[1] * local_std + local_mean
            else:
                corrected_raw = local_data[1]
        else:
            corrected_raw = None
        local_output[dataset_index] = {'corrected_raw': corrected_raw,
                                       'corrected_data': corrected_data}

    with output_lock:
        index_list = list(local_output.keys())
        for data_index in index_list:
            output_dict[data_index] = local_output.pop(data_index)


def write_output_to_file(output_dict,
                         output_file_path,
                         raw_dataset_name,
                         output_dataset_name,
                         batch_size):
    index_list = list(output_dict.keys())

    with h5py.File(output_file_path, 'a') as out_file:
        raw_out = out_file[raw_dataset_name]
        dset_out = out_file[output_dataset_name]
        for dataset_index in index_list:
            dataset = output_dict.pop(dataset_index)
            local_size = dataset['corrected_data'].shape[0]
            start = dataset_index * batch_size
            end = dataset_index * batch_size + local_size
            dset_out[start:end, :] = dataset['corrected_data']

            if dataset['corrected_raw'] is not None:
                raw_out[
                        dataset_index
                        * batch_size:dataset_index
                        * batch_size
                        + local_size,
                        :,
                 ] = dataset['corrected_raw']

    return output_dict


class core_inferrence:
    # This is the generic inferrence class
    def __init__(self, inferrence_json_path, generator_obj):
        self.inferrence_json_path = inferrence_json_path
        self.generator_obj = generator_obj

        local_json_loader = JsonLoader(inferrence_json_path)
        local_json_loader.load_json()
        self.json_data = local_json_loader.json_data

        self.output_file = self.json_data["output_file"]

        if "save_raw" in self.json_data.keys():
            self.save_raw = self.json_data["save_raw"]
        else:
            self.save_raw = False

        if "rescale" in self.json_data.keys():
            self.rescale = self.json_data["rescale"]
        else:
            self.rescale = True

        self.batch_size = self.generator_obj.batch_size
        self.nb_datasets = len(self.generator_obj)
        self.indiv_shape = self.generator_obj.get_output_size()

    def run(self):
        sfd_t0 = time.time()
        print("starting core inference")
        print(f"nb_datasets: {self.nb_datasets}")
        print(f"batch_size: {self.batch_size}")
        final_shape = [self.nb_datasets * self.batch_size]
        final_shape.extend(self.indiv_shape)

        chunk_size = [1]
        chunk_size.extend(self.indiv_shape)

        n_parallel_workers = self.json_data['n_parallel_workers']

        mgr = multiprocessing.Manager()
        output_dict = mgr.dict()
        output_lock = mgr.Lock()
        process_list = []


        output_dataset_name = "data"
        raw_dataset_name = "raw"

        with h5py.File(self.output_file, "w") as file_handle:
            file_handle.create_dataset(
                output_dataset_name,
                shape=tuple(final_shape),
                chunks=tuple(chunk_size),
                dtype="float32",
            )

            if self.save_raw:
                file_handle.create_dataset(
                    raw_dataset_name,
                    shape=tuple(final_shape),
                    chunks=tuple(chunk_size),
                    dtype="float32",
                )

        for index_dataset in np.arange(0, self.nb_datasets, 1):
            local_data = self.generator_obj.__getitem__(index_dataset)
            local_mean, local_std = \
                self.generator_obj.__get_norm_parameters__(index_dataset)

            this_batch = dict()
            this_batch[index_dataset] = dict()
            this_batch[index_dataset]['local_data'] = local_data
            this_batch[index_dataset]['local_mean'] = local_mean
            this_batch[index_dataset]['local_std'] = local_std

            process = multiprocessing.Process(
                        target=core_inference_worker,
                        args=(self.json_data,
                              this_batch,
                              self.rescale,
                              self.save_raw,
                              output_dict,
                              output_lock))

            process.start()
            process_list.append(process)
            while len(process_list) >= n_parallel_workers:
                process_list = _winnow_process_list(process_list)

            if len(output_dict) >= max(1, self.nb_datasets//8):
                with output_lock:
                    write_output_to_file(
                        output_dict,
                        self.output_file,
                        raw_dataset_name,
                        output_dataset_name,
                        self.batch_size)

        for p in process_list:
            p.join()

        write_output_to_file(
            output_dict,
            self.output_file,
            raw_dataset_name,
            output_dataset_name,
            self.batch_size)

        print("done with core_inference")
        duration = time.time()-sfd_t0
        print(f"core_inference took {duration:.2e} seconds")
