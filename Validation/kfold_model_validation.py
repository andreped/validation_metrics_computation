import os
import subprocess, shutil
import multiprocessing
import itertools
import traceback

import numpy as np
import csv
import time
import nibabel as nib
from nibabel import four_to_three
from copy import deepcopy
import pandas as pd
from math import ceil

from setuptools.command.setopt import option_base
from tqdm import tqdm

from Computation.dice_computation import separate_dice_computation
from Validation.instance_segmentation_validation import *
from Utils.resources import SharedResources
from Utils.PatientMetricsStructure import PatientMetrics
from Utils.io_converters import get_fold_from_file
from Validation.validation_utilities import best_segmentation_probability_threshold_analysis, compute_fold_average
from Validation.extra_metrics_computation import compute_extra_metrics, compute_overall_metrics_correlation


# def compute_dice(volume1, volume2):
#     dice = 0.
#     if np.sum(volume1[volume2 == 1]) != 0:
#         dice = (np.sum(volume1[volume2 == 1]) * 2.0) / (np.sum(volume1) + np.sum(volume2))
#     return dice
#
#
# def compute_dice_uncertain(volume1, volume2, epsilon=0.1):
#     dice = (np.sum(volume1[volume2 == 1]) * 2.0 + epsilon) / (np.sum(volume1) + np.sum(volume2) + epsilon)
#     return dice
#
#
# def separate_dice_computation(args):
#     """
#     Dice computation method linked to the multiprocessing strategy. Effectively where the call to compute is made.
#     :param args: list of arguments split from the lists given to the multiprocessing.Pool call.
#     :return: list with the computed results for the current patient, at the given probability threshold.
#     """
#     t = np.round(args[0], 2)
#     fold_number = args[1]
#     gt = args[2]
#     detection_ni = args[3]
#     patient_id = args[4]
#     results = []
#
#     detection = deepcopy(detection_ni.get_data())
#     detection[detection < t] = 0
#     detection[detection >= t] = 1
#     detection = detection.astype('uint8')
#     dice = compute_dice(gt, detection)
#
#     obj_val = InstanceSegmentationValidation(gt_image=gt, detection_image=detection)
#     try:
#         # obj_val.set_trace_parameters(self.output_folder, fold_number, patient, t)
#         obj_val.spacing = detection_ni.header.get_zooms()
#         # @TODO. Have to find a way to disable it from config file, can't compute it for like airways, too many small elements
#         obj_val.run()
#     except Exception as e:
#         print('Issue computing instance segmentation parameters for patient {}'.format(patient_id))
#         print(traceback.format_exc())
#
#     instance_results = obj_val.instance_detection_results
#     results.append([fold_number, patient_id, t, dice] + instance_results + [len(obj_val.gt_candidates),
#                                                                             len(obj_val.detection_candidates)])
#
#     return results


class ModelValidation:
    """
    Compute performances metrics after k-fold cross-validation from sets of inference.
    The results will be stored inside a Validation sub-folder placed within the provided destination directory.
    """
    def __init__(self):
        self.data_root = SharedResources.getInstance().data_root
        self.input_folder = SharedResources.getInstance().validation_input_folder
        base_output_folder = SharedResources.getInstance().validation_output_folder

        if base_output_folder is not None and base_output_folder != "":
            self.output_folder = os.path.join(base_output_folder, 'Validation')
        else:
            self.output_folder = os.path.join(self.input_folder, 'Validation')
        os.makedirs(self.output_folder, exist_ok=True)

        self.fold_number = SharedResources.getInstance().validation_nb_folds
        self.split_way = SharedResources.getInstance().validation_split_way
        self.metric_names = []
        self.metric_names.extend(SharedResources.getInstance().validation_metric_names)
        self.detection_overlap_thresholds = SharedResources.getInstance().validation_detection_overlap_thresholds
        print("Detection overlap: ", self.detection_overlap_thresholds)
        self.gt_files_suffix = SharedResources.getInstance().validation_gt_files_suffix
        self.prediction_files_suffix = SharedResources.getInstance().validation_prediction_files_suffix

    def run(self):
        self.__generate_dice_scores()
        class_optimal = best_segmentation_probability_threshold_analysis(self.input_folder,
                                                                         detection_overlap_thresholds=self.detection_overlap_thresholds)
        # compute_extra_metrics(self.data_root, self.input_folder, nb_folds=self.fold_number, split_way=self.split_way,
        #                       optimal_threshold=optimal_threshold, metrics=self.metric_names,
        #                       gt_files_suffix=self.gt_files_suffix,
        #                       prediction_files_suffix=self.prediction_files_suffix)
        compute_fold_average(self.input_folder, class_optimal=class_optimal, metrics=self.metric_names)
        # compute_overall_metrics_correlation(self.input_folder, best_threshold=optimal_threshold)

    def __generate_dice_scores(self):
        """
        Generate the Dice scores (and default instance detection metrics) for all the patients and 10 probability
        thresholds equally-spaced. All the computed results will be stored inside all_dice_scores.csv.
        The results are saved after each patient, making it possible to resume the computation if a crash occurred.
        @TODO. Include an override flag to recompute anyway.
        :return:
        """
        cross_validation_description_file = os.path.join(self.input_folder, 'cross_validation_folds.txt')
        self.results_df = []
        self.class_results_df = {}
        self.dice_output_filename = os.path.join(self.output_folder, 'all_dice_scores.csv')
        self.class_dice_output_filenames = {}
        for c in SharedResources.getInstance().validation_class_names:
            self.class_dice_output_filenames[c] = os.path.join(self.output_folder, c + '_dice_scores.csv')
            self.class_results_df[c] = []
        self.results_df_base_columns = ['Fold', 'Patient', 'Threshold']
        self.results_df_base_columns.extend(["PiW Dice", "PiW Recall", "PiW Precision", "PiW F1"])
        self.results_df_base_columns.extend(["PaW Dice", "PaW Recall", "PaW Precision", "PaW F1"])
        self.results_df_base_columns.extend(["GT volume (ml)", "True Positive", "Detection volume (ml)"])
        self.results_df_base_columns.extend(["OW Dice", "OW Recall", "OW Precision", "OW F1", '#GT', '#Det'])

        if not os.path.exists(self.dice_output_filename):
            self.results_df = pd.DataFrame(columns=self.results_df_base_columns)
            for c in SharedResources.getInstance().validation_class_names:
                self.class_results_df[c] = pd.DataFrame(columns=self.results_df_base_columns)
        else:
            self.results_df = pd.read_csv(self.dice_output_filename)
            if self.results_df.columns[0] != 'Fold':
                self.results_df = pd.read_csv(self.dice_output_filename, index_col=0)
            for c in SharedResources.getInstance().validation_class_names:
                self.class_results_df[c] = pd.read_csv(self.class_dice_output_filenames[c])
                if self.class_results_df[c].columns[0] != 'Fold':
                    self.class_results_df[c] = pd.read_csv(self.class_dice_output_filenames[c], index_col=0)

        self.results_df['Patient'] = self.results_df.Patient.astype(str)
        for c in SharedResources.getInstance().validation_class_names:
            self.class_results_df[c]['Patient'] = self.class_results_df[c].Patient.astype(str)

        results_per_folds = []
        for fold in range(0, self.fold_number):
            print('\nProcessing fold {}/{}.\n'.format(fold, self.fold_number - 1))
            if self.split_way == 'two-way':
                test_set, _ = get_fold_from_file(filename=cross_validation_description_file, fold_number=fold)
            else:
                val_set, test_set = get_fold_from_file(filename=cross_validation_description_file, fold_number=fold)
            results = self.__generate_dice_scores_for_fold(data_list=test_set, fold_number=fold)
            results_per_folds.append(results)

    def __generate_dice_scores_for_fold(self, data_list, fold_number):
        for i, patient in enumerate(tqdm(data_list)):
            uid = None
            try:
                start = time.time()
                # @TODO. Hard-coded, have to decide on naming convention....
                # Working for files generated with DBUtils code, having a proper name.
                uid = patient.split('_')[1]
                sub_folder_index = str(ceil(int(uid) / 200))  # patient.split('_')[0]
                patient_extended = '_'.join(patient.split('_')[1:-1]).strip()

                # For cross validation files with non-proper names
                # uid = patient.split('_')[0]
                # sub_folder_index = str(ceil(int(uid) / 200))
                # patient_extended = ""

                # Placeholder for holding all metrics for the current patient
                patient_metrics = PatientMetrics(id=uid, class_names=SharedResources.getInstance().validation_class_names)
                patient_metrics.init_from_file(self.output_folder)
                # Checking if values have already been computed for the current patient to skip it if so.
                if patient_metrics.is_complete():
                    continue

                success = self.__identify_patient_files(patient_metrics, sub_folder_index, fold_number)
                if not success:
                    print('Input files not found for patient {}\n'.format(uid))
                    continue

                self.__generate_dice_scores_for_patient(patient_metrics, fold_number)

                # # Checking if values have already been computed for the current patient to skip it if so.
                # # In case values were not properly computed for the core part (i.e. first 10 columns without
                # # extra-metrics), a recompute will be triggered.
                # if len(self.results_df.loc[self.results_df['Patient'] == uid]) != 0:
                #     if not None in self.results_df.loc[self.results_df['Patient'] == uid].values[0] and not np.isnan(
                #             np.sum(self.results_df.loc[self.results_df['Patient'] == uid].values[0][3:10])):
                #         continue
                #
                # # Annoying, but independent of extension
                # # @TODO. must load images with SimpleITK to be completely generic.
                # detection_image_base = os.path.join(self.input_folder, 'predictions', str(fold_number),
                #                                   sub_folder_index + '_' + uid)
                # detection_filename = None
                # for _, _, files in os.walk(detection_image_base):
                #     for f in files:
                #         if self.prediction_files_suffix in f:
                #             detection_filename = os.path.join(detection_image_base, f)
                #     break
                # if not os.path.exists(detection_filename):
                #     continue
                #
                # # @TODO. Second piece added to make it work when names are wrong in the cross validation file.
                # patient_extended = os.path.basename(detection_filename).split(self.prediction_files_suffix)[0][:-1]
                # patient_image_base = os.path.join(self.data_root, sub_folder_index, uid, 'volumes', patient_extended)
                # patient_image_filename = None
                # for _, _, files in os.walk(os.path.dirname(patient_image_base)):
                #     for f in files:
                #         if os.path.basename(patient_image_base) in f:
                #             patient_image_filename = os.path.join(os.path.dirname(patient_image_base), f)
                #     break
                #
                # ground_truth_base = os.path.join(self.data_root, sub_folder_index, uid, 'segmentations', patient_extended)
                # ground_truth_filename = None
                # for _, _, files in os.walk(os.path.dirname(ground_truth_base)):
                #     for f in files:
                #         if os.path.basename(ground_truth_base) in f and self.gt_files_suffix in f:
                #             ground_truth_filename = os.path.join(os.path.dirname(ground_truth_base), f)
                #     break
                #
                # file_stats = os.stat(detection_filename)
                # ground_truth_ni = nib.load(ground_truth_filename)
                # if len(ground_truth_ni.shape) == 4:
                #     ground_truth_ni = nib.four_to_three(ground_truth_ni)[0]
                #
                # if file_stats.st_size == 0:
                #     nib.save(nib.Nifti1Image(np.zeros(ground_truth_ni.get_shape), affine=ground_truth_ni.affine),
                #              detection_filename)
                #
                # detection_ni = nib.load(detection_filename)
                # if detection_ni.shape != ground_truth_ni.shape:
                #     continue
                #
                # gt = ground_truth_ni.get_data()
                # gt[gt >= 1] = 1
                #
                # pat_results = []
                # thr_range = np.arange(0.1, 1.1, 0.1)
                # if SharedResources.getInstance().number_processes > 1:
                #     pool = multiprocessing.Pool(processes=SharedResources.getInstance().number_processes)
                #     pat_results = pool.map(separate_dice_computation, zip(thr_range,
                #                                                           itertools.repeat(fold_number),
                #                                                           itertools.repeat(gt),
                #                                                           itertools.repeat(detection_ni),
                #                                                           itertools.repeat(uid)
                #                                                           )
                #                            )
                #     pool.close()
                #     pool.join()
                # else:
                #     for thr_value in thr_range:
                #         thr_res = separate_dice_computation([thr_value, fold_number, gt, detection_ni, uid])
                #         pat_results.append(thr_res)
                #
                # for ind, th in enumerate(thr_range):
                #     sub_df = self.results_df.loc[(self.results_df['Patient'] == uid) & (self.results_df['Fold'] == fold_number) & (self.results_df['Threshold'] == th)]
                #     ind_values = np.asarray(pat_results).reshape((len(thr_range), len(self.results_df_base_columns)))[ind, :]
                #     buff_df = pd.DataFrame(ind_values.reshape(1, len(self.results_df_base_columns)),
                #                            columns=list(self.results_df_base_columns))
                #     if len(sub_df) == 0:
                #         self.results_df = self.results_df.append(buff_df, ignore_index=True)
                #     else:
                #         self.results_df.loc[sub_df.index.values[0], :] = list(ind_values)
                # self.results_df.to_csv(self.dice_output_filename, index=False)
            except Exception as e:
                print('Issue processing patient {}\n'.format(uid))
                print(traceback.format_exc())
                continue
        return 0

    def __identify_patient_files(self, patient_metrics, folder_index, fold_number):
        """
        Asserts the existence of the raw files on disk for computing the metrics for the current patient.
        :return:
        """
        uid = patient_metrics.unique_id
        classes = SharedResources.getInstance().validation_class_names
        nb_classes = len(classes)
        patient_filenames = {}

        # Iterating over all classes, where independent files are expected
        for c in range(nb_classes):
            patient_filenames[classes[c]] = []
            gt_suffix = self.gt_files_suffix[c]
            pred_suffix = self.prediction_files_suffix[c]

            # Annoying, but independent of extension
            # @TODO. must load images with SimpleITK to be completely generic.
            detection_image_base = os.path.join(self.input_folder, 'predictions', str(fold_number),
                                                folder_index + '_' + uid)
            detection_filename = None
            for _, _, files in os.walk(detection_image_base):
                for f in files:
                    if pred_suffix in f:
                        detection_filename = os.path.join(detection_image_base, f)
                break
            if not os.path.exists(detection_filename):
                print("No detection file found for class {} in patient {}".format(c, patient_metrics.unique_id))
                return False

            # @TODO. Second piece added to make it work when names are wrong in the cross validation file.
            patient_extended = os.path.basename(detection_filename).split(pred_suffix)[0][:-1]
            patient_image_base = os.path.join(self.data_root, folder_index, uid, 'volumes', patient_extended)
            patient_image_filename = None
            for _, _, files in os.walk(os.path.dirname(patient_image_base)):
                for f in files:
                    if os.path.basename(patient_image_base) in f:
                        patient_image_filename = os.path.join(os.path.dirname(patient_image_base), f)
                break

            ground_truth_base = os.path.join(self.data_root, folder_index, uid, 'segmentations', patient_extended)
            ground_truth_filename = None
            for _, _, files in os.walk(os.path.dirname(ground_truth_base)):
                for f in files:
                    if os.path.basename(ground_truth_base) in f and gt_suffix in f:
                        ground_truth_filename = os.path.join(os.path.dirname(ground_truth_base), f)
                break

            detection_ni = nib.load(detection_filename)
            # If there's no ground truth, we assume the class to be empty for this patient and create an
            # empty ground truth volume.
            if ground_truth_filename is None or not os.path.exists(ground_truth_filename):
                empty_gt = np.zeros(detection_ni.get_data().shape)
                ground_truth_filename = os.path.join(os.path.dirname(detection_filename), uid + "_groundtruth_" + classes[c] + ".nii.gz")
                nib.save(nib.Nifti1Image(empty_gt, detection_ni.affine), ground_truth_filename)
            else:
                file_stats = os.stat(detection_filename)
                ground_truth_ni = nib.load(ground_truth_filename)
                if len(ground_truth_ni.shape) == 4:
                    ground_truth_ni = nib.four_to_three(ground_truth_ni)[0]

                if file_stats.st_size == 0:
                    nib.save(nib.Nifti1Image(np.zeros(ground_truth_ni.get_shape), affine=ground_truth_ni.affine),
                             detection_filename)

                if detection_ni.shape != ground_truth_ni.shape:
                    return False

            patient_filenames[classes[c]] = [ground_truth_filename, detection_filename]
        patient_metrics.set_patient_filenames(patient_filenames)
        return True

    def __generate_dice_scores_for_patient(self, patient_metrics, fold_number):
        """
        Compute the basic metrics for all classes of the current patient
        :return:
        """
        uid = patient_metrics.unique_id
        classes = SharedResources.getInstance().validation_class_names
        nb_classes = len(classes)
        patient_filenames = {}
        thr_range = np.arange(0.1, 1.1, 0.1)

        # Iterating over all classes, where independent files are expected
        for c in range(nb_classes):
            gt_filename, det_filename = patient_metrics.get_class_filenames(c)
            ground_truth_ni = nib.load(gt_filename)
            detection_ni = nib.load(det_filename)

            gt = ground_truth_ni.get_data()
            gt[gt >= 1] = 1

            class_tp_threshold = SharedResources.getInstance().validation_true_positive_volume_thresholds[c]
            gt_volume = np.count_nonzero(gt) * np.prod(ground_truth_ni.header.get_zooms()) * 1e-3
            tp_state = True if gt_volume > class_tp_threshold else False
            extra = [np.round(gt_volume, 4), tp_state]
            pat_results = []
            if SharedResources.getInstance().number_processes > 1:
                pool = multiprocessing.Pool(processes=SharedResources.getInstance().number_processes)
                pat_results = pool.map(separate_dice_computation, zip(thr_range,
                                                                      itertools.repeat(fold_number),
                                                                      itertools.repeat(gt),
                                                                      itertools.repeat(detection_ni),
                                                                      itertools.repeat(uid),
                                                                      itertools.repeat(extra)
                                                                      )
                                       )
                pool.close()
                pool.join()
            else:
                for thr_value in thr_range:
                    thr_res = separate_dice_computation([thr_value, fold_number, gt, detection_ni, uid, extra])
                    pat_results.append(thr_res)

            patient_metrics.set_class_metrics(classes[c], pat_results)
            # Filling in the csv files on disk for faster resume
            class_results_filename = self.class_dice_output_filenames[classes[c]]
            for ind, th in enumerate(thr_range):
                sub_df = self.class_results_df[classes[c]].loc[
                    (self.class_results_df[classes[c]]['Patient'] == uid) & (self.class_results_df[classes[c]]['Fold'] == fold_number) & (
                            self.class_results_df[classes[c]]['Threshold'] == th)]
                ind_values = np.asarray(pat_results[ind])
                buff_df = pd.DataFrame(ind_values.reshape(1, len(self.results_df_base_columns)),
                                       columns=list(self.results_df_base_columns))
                if len(sub_df) == 0:
                    self.class_results_df[classes[c]] = self.class_results_df[classes[c]].append(buff_df, ignore_index=True)
                else:
                    self.class_results_df[classes[c]].loc[sub_df.index.values[0], :] = list(ind_values)
            self.class_results_df[classes[c]].to_csv(class_results_filename, index=False)

        # Should compute the class macro-average results if multiple classes
        class_averaged_results = None
        class_results = []
        for c in classes:
            pat_class_results = patient_metrics.get_class_metrics(c)
            class_results.append(pat_class_results)
        class_averaged_results = np.average(np.asarray(class_results)[:, :, 1:], axis=0)

        # Filling in the csv files on disk for faster resume
        for ind, th in enumerate(thr_range):
            sub_df = self.results_df.loc[
                (self.results_df['Patient'] == uid) & (self.results_df['Fold'] == fold_number) & (
                            self.results_df['Threshold'] == th)]
            ind_values = np.asarray([fold_number, uid, np.round(th, 2)] + list(class_averaged_results[ind]))
            buff_df = pd.DataFrame(ind_values.reshape(1, len(self.results_df_base_columns)),
                                   columns=list(self.results_df_base_columns))
            if len(sub_df) == 0:
                self.results_df = self.results_df.append(buff_df, ignore_index=True)
            else:
                self.results_df.loc[sub_df.index.values[0], :] = list(ind_values)
        self.results_df.to_csv(self.dice_output_filename, index=False)
