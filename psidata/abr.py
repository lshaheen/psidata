'''
Interface for reading tone-evoked ABR data generated by psiexperiment
'''
import logging
log = logging.getLogger(__name__)

from functools import lru_cache, partialmethod
import os.path
import shutil
import re
from glob import glob

import bcolz
import numpy as np
import pandas as pd
from scipy import signal

from .bcolz_tools import (BcolzRecording, BcolzSignal, load_ctable_as_df,
                          repair_carray_size)


# Max size of LRU cache
MAXSIZE = 1024


MERGE_PATTERN = \
    r'\g<date>-* ' \
    r'\g<experimenter> ' \
    r'\g<animal> ' \
    r'\g<ear> ' \
    r'\g<note> ' \
    r'\g<experiment>*'


def cache(f, name=None):
    import inspect
    s = inspect.signature(f)
    if name is None:
        name = f.__code__.co_name

    def wrapper(self, *args, refresh_cache=False, **kwargs):
        bound_args = s.bind(self, *args, **kwargs)
        bound_args.apply_defaults()
        iterable = bound_args.arguments.items()
        file_params = ', '.join(f'{k}={v}' for k, v in iterable if k != 'self')
        file_name = f'{name} {file_params}.pkl'

        cache_path = self.base_path / 'cache'
        cache_path.mkdir(parents=True, exist_ok=True)
        cache_file = cache_path / file_name

        if not refresh_cache and cache_file.exists():
            result = pd.read_pickle(cache_file)
        else:
            result = f(self, *args, **kwargs)
            result.to_pickle(cache_file)

        return result

    return wrapper


class ABRFile(BcolzRecording):

    def __init__(self, *args, **kw):
        super().__init__(*args, **kw)
        if 'eeg' not in self.carray_names:
            raise ValueError('Missing eeg data')
        if 'erp_metadata' not in self.ctable_names:
            raise ValueError('Missing erp metadata')

    @property
    @lru_cache(maxsize=MAXSIZE)
    def eeg(self):
        # Load and ensure that the EEG data is fine. If not, repair it and
        # reload the data.
        rootdir = self.base_folder / 'eeg'
        eeg = bcolz.carray(rootdir=rootdir)
        if len(eeg) == 0:
            log.debug('EEG for %s is corrupt. Repairing.', self.base_folder)
            repair_carray_size(rootdir)
        return BcolzSignal(rootdir)

    @property
    @lru_cache(maxsize=MAXSIZE)
    def erp_metadata(self):
        data = self._load_bcolz('erp_metadata')
        return data.rename(columns=lambda x: x.replace('target_tone_', ''))

    @cache
    def get_epochs(self, offset=0, duration=8.5e-3, detrend='constant',
                   reject_threshold=None, reject_mode='absolute',
                   columns='auto'):
        fn = self.eeg.get_epochs
        result = fn(self.erp_metadata, offset, duration, detrend, columns)
        return self._apply_reject(result, reject_threshold, reject_mode)

    @cache
    def get_random_segments(self, n, offset=0, duration=8.5e-3,
                            detrend='constant', reject_threshold=None,
                            reject_mode='absolute'):
        fn = self.eeg.get_random_segments
        result = fn(n, offset, duration, detrend)
        return self._apply_reject(result, reject_threshold, reject_mode)

    @cache
    def get_epochs_filtered(self, filter_lb=300, filter_ub=3000,
                            filter_order=1, offset=-1e-3, duration=10e-3,
                            detrend='constant', pad_duration=10e-3,
                            reject_threshold=None, reject_mode='absolute',
                            columns='auto'):
        fn = self.eeg.get_epochs_filtered
        result = fn(self.erp_metadata, offset, duration, filter_lb, filter_ub,
                    filter_order, detrend, pad_duration, columns)
        return self._apply_reject(result, reject_threshold, reject_mode)

    @cache
    def get_random_segments_filtered(self, n, filter_lb=300, filter_ub=3000,
                                     filter_order=1, offset=-1e-3,
                                     duration=10e-3, detrend='constant',
                                     pad_duration=10e-3,
                                     reject_threshold=None,
                                     reject_mode='absolute'):

        fn = self.eeg.get_random_segments_filtered
        result = fn(n, offset, duration, filter_lb, filter_ub, filter_order,
                    detrend, pad_duration)
        return self._apply_reject(result, reject_threshold, reject_mode)

    def _apply_reject(self, result, reject_threshold, reject_mode):
        result = result.dropna()

        if reject_threshold is None:
            # 'reject_mode' wasn't added until a later version of the ABR
            # program, so we set it to the default that was used before if not
            # present.
            row = self.erp_metadata.loc[0]
            reject_threshold = row['reject_threshold']
            reject_mode = row.get('reject_mode', 'absolute')

        if reject_threshold is not np.inf:
            # No point doing this if reject_threshold is infinite.
            if reject_mode == 'absolute':
                m = (result < reject_threshold).all(axis=1)
                result = result.loc[m]
            elif reject_mode == 'amplitude':
                # TODO
                raise NotImplementedError

        return result


class ABRSupersetFile:

    def __init__(self, *base_folders):
        self._fh = [ABRFile(base_folder) for base_folder in base_folders]

    def _merge_results(self, fn_name, *args, **kwargs):
        result_set = [getattr(fh, fn_name)(*args, **kwargs) for fh in self._fh]
        return pd.concat(result_set, keys=range(len(self._fh)), names=['file'])

    get_epochs = partialmethod(_merge_results, 'get_epochs')
    get_epochs_filtered = partialmethod(_merge_results, 'get_epochs_filtered')
    get_random_segments = partialmethod(_merge_results, 'get_random_segments')
    get_random_segments_filtered = \
        partialmethod(_merge_results, 'get_random_segments_filtered')

    @classmethod
    def from_pattern(cls, base_folder):
        head, tail = os.path.split(base_folder)
        glob_tail = FILE_RE.sub(MERGE_PATTERN, tail)
        glob_pattern = os.path.join(head, glob_tail)
        folders = glob(glob_pattern)
        inst = cls(*folders)
        inst._base_folder = base_folder
        return inst

    @classmethod
    def from_folder(cls, base_folder):
        folders = [os.path.join(base_folder, f) \
                   for f in os.listdir(base_folder)]
        inst = cls(*[f for f in folders if os.path.isdir(f)])
        inst._base_folder = base_folder
        return inst

    @property
    def erp_metadata(self):
        result_set = [fh.erp_metadata for fh in self._fh]
        return pd.concat(result_set, keys=range(len(self._fh)), names=['file'])


def load(base_folder):
    check = os.path.join(base_folder, 'erp')
    if os.path.exists(check):
        return ABRFile(base_folder)
    else:
        return ABRSupersetFile.from_folder(base_folder)


def is_abr_experiment(path):
    try:
        load(path)
        return True
    except Exception:
        return False
