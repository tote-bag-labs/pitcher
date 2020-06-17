import click
import numpy as np
import scipy as sp
import audiofile as af

from librosa.effects import time_stretch
from librosa.core import resample
from librosa import load

from SAR import SAR, normalize_input
from numba import jit
from pyrubberband import pyrb

POSITIVE_TUNING_RATIO = 1.02930223664
# TODO: maybe revisit, "sp-12 has 32 different tuning settings with various skip amounts"
# according to paper looks like skip of ~0.64062 creates nice aliasing?
# these all look like a skip of ~ 0.06
NEGATIVE_TUNING_RATIOS = {-1: 1.05652677103003,
                          -2: 1.1215356033380033,
                          -3: 1.1834835840896631,
                          -4: 1.253228360845465,
                          -5: 1.3310440397149297,
                          -6: 1.4039714929646099,
                          -7: 1.5028019735639886,
                          -8: 1.5766735700797954}

QUANTIZATION_BITS = 12
QUANTIZATION_LEVELS = 2 ** QUANTIZATION_BITS
U = 1  # max Amplitude to be quantized TODO: revisit?
DELTA_S = 2 * U / QUANTIZATION_LEVELS  # level distance

S_MIDRISE = -U + DELTA_S / 2 + np.arange(QUANTIZATION_LEVELS) * DELTA_S
S_MIDTREAD = -U + np.arange(QUANTIZATION_LEVELS) * DELTA_S

RESAMPLE_MULTIPLIER = 2
ZERO_ORDER_HOLD_MULTIPLIER = 4

INPUT_SAMPLE_RATE = 96000
OUTPUT_SAMPLE_RATE = 48000
TARGET_SAMPLE_RATE = 26040

TARGET_SAMPLE_RATE_MULTIPLE = TARGET_SAMPLE_RATE * RESAMPLE_MULTIPLIER

# map these if they grow any longer
PITCH_METHODS = ['manual', 'rubberband']
RESAMPLE_METHODS = ['librosa', 'scipy']


def manual_pitch(x, st):
    if (0 > st >= -8):
        t = NEGATIVE_TUNING_RATIOS[st]
    elif st > 0:
        t = POSITIVE_TUNING_RATIO ** -st
    elif st == 0:  # no change
        return x
    else:  # -8 > st: extrapolate, loses a few points of percision for some reason
        f = sp.interpolate.interp1d(list(NEGATIVE_TUNING_RATIOS.keys()),
                                    list(NEGATIVE_TUNING_RATIOS.values()),
                                    fill_value='extrapolate')
        t = f(st)

    n = int(np.round(len(x) * t))
    # - 1 accounts for rounding issues
    r = np.linspace(0, len(x) - 1, n).round().astype(np.int32)
    pitched = [x[r[e]] for e in range(n-1)]  # could yield here
    return pitched


def filter_input(x):
    # approximating the anti aliasing filter, don't think this needs to be
    # perfect since at fs/2=13.02kHz only -10dB attenuation, might be able to
    # improve accuracy in the 15 -> 20kHz range with firwin
    f = sp.signal.ellip(4, 1, 72, 0.666, analog=False, output='sos')
    y = sp.signal.sosfilt(f, x)
    return y


def filter_output(x):
    # use window method to replicate the fixed output filter
    freq = np.array([0, 6510, 8000, 10000, 11111, 13020, 15000, 17500, 20000, 24000])
    att = np.array([0, 0, -5, -10, -15, -23, -28, -35, -41, -40])
    gain = np.power(10, att/20)
    f = sp.signal.firwin2(45, freq, gain, fs=OUTPUT_SAMPLE_RATE, antisymmetric=False)
    sos = sp.signal.tf2sos(f, [1.0])
    y = sp.signal.sosfilt(sos, x)
    return y


def pyrb_pitch(y, st):
    t = POSITIVE_TUNING_RATIO ** st  # revisit when replacing pyrb
    pitched = pyrb.pitch_shift(y, TARGET_SAMPLE_RATE, n_steps=st)
    return time_stretch(pitched, t)


def librosa_resample(y):
    # http://www.synthark.org/Archive/EmulatorArchive/SP1200.html
    # "...resample to a multiple of the SP-12(00)'s sampling rate..."
    resampled = resample(y, INPUT_SAMPLE_RATE, TARGET_SAMPLE_RATE_MULTIPLE)
    # "...then downsample to the SP-12(00) rate"
    downsampled = resample(resampled, TARGET_SAMPLE_RATE_MULTIPLE, TARGET_SAMPLE_RATE)
    return downsampled


def scipy_resample(y):
    seconds = len(y)/INPUT_SAMPLE_RATE
    target_samples = int(seconds * TARGET_SAMPLE_RATE_MULTIPLE) + 1
    resampled = sp.signal.resample(y, target_samples)
    decimated = sp.signal.decimate(resampled, RESAMPLE_MULTIPLIER)
    return decimated


def zero_order_hold(y):
    # intentionally oversample by repeating each sample 4 times
    # could also try a freq aliased sinc filter
    return np.repeat(y, ZERO_ORDER_HOLD_MULTIPLIER)


# we'd like output to be measured in the same units as x, just quantized
# rescaling currently isn't working, maybe revisit this
# peaks also seem slightly lower using this vs original quantize method
def sar_quantize(x):
    ncomp = 0.001  # noise of the comparator
    ndac = 0  # noise of the C-DAC
    nsamp = 0  # sampling kT/C noise

    myadc = SAR(QUANTIZATION_BITS, ncomp, ndac, nsamp, 2)
    normalized, center, maxbin = normalize_input(x)
    print(normalized)
    # run adc
    adcout = myadc.sarloop(normalized)
    _ = ''  # throwaway, don't need center or maxbin here
    adcout, _, _, normalize_input(adcout)
    # rescale to original
    rescaled = adcout * maxbin + center
    print(rescaled)
    return rescaled


# interp1d type='nearest' might be a faster way
def quantize(x, S):

    X = x.reshape((-1, 1)).astype(np.float32)
    S = S.reshape((1, -1)).astype(np.float32)  # don't think this is necessary

    @jit(nopython=True)
    def nearest_value(X, S):
        y = np.zeros(len(X), dtype=np.int32)
        for i, item in enumerate(X):
            y[i] = np.abs(item-S).argmin()
        return y

    y = []
    X = np.asfortranarray(X)
    S = np.asfortranarray(S)
    y = nearest_value(X, S)
    quantized = S.flat[y].reshape(x.shape)
    return quantized


# similar to sar, output of digitize would need to be rescaled back to original
# we'd like output to be measured in the same units as x, just quantized
def digitize(x, S):
    y = np.digitize(x.flatten(), S.flatten())
    return y


# TODO
# - requirements
# - readme
# - impletement vcf? (ring moog)
# - improve input filter fit
# - logging
# - revisit pitch values
# - replace or delete pyrb
# - replace librosa if there is a module with better performance
# - supress librosa numba warning

# NOTES:
# - could use sosfiltfilt for zero phase filtering, but it doubles filter order

# Based on:
# https://ccrma.stanford.edu/~dtyeh/sp12/yeh2007icmcsp12slides.pdf

# signal path: input filter > sample & hold > 12 bit quantizer > pitching
# & decay > zero order hold > optional eq filters > output filter

@click.command()
@click.option('--file', required=True)
@click.option('--st', default=0, help='number of semitones to shift')
@click.option('--pitch-method', default='manual')
@click.option('--resample-method', default='scipy')
@click.option('--output-file', required=True)
@click.option('--skip-input-filter', is_flag=True, default=False)
@click.option('--skip-output-filter', is_flag=True, default=False)
@click.option('--skip-quantize', is_flag=True, default=False)
@click.option('--normalize', is_flag=True, default=False)
def pitch(file, st, pitch_method, resample_method, output_file,
          skip_input_filter, skip_output_filter, skip_quantize,
          normalize):

    # oversample to 96khz
    y, s = load(file, sr=INPUT_SAMPLE_RATE)

    if not skip_input_filter:
        # anti aliasing filter
        y = filter_input(y)

    if resample_method in RESAMPLE_METHODS:
        # resample to target sample rate
        if resample_method == RESAMPLE_METHODS[0]:
            resampled = librosa_resample(y)
        elif resample_method == RESAMPLE_METHODS[1]:
            resampled = scipy_resample(y)
    else:
        raise ValueError('invalid resample method, '
                         f'valid methods are {RESAMPLE_METHODS}')

    if not skip_quantize:
        # simulate analog -> digital conversion
        resampled = quantize(resampled, S_MIDRISE)  # TODO: midtread or midrise?

    if pitch_method in PITCH_METHODS:
        if pitch_method == PITCH_METHODS[0]:
            pitched = manual_pitch(resampled, st)
        elif pitch_method == PITCH_METHODS[1]:
            pitched = pyrb_pitch(resampled, st)
    else:
        raise ValueError('invalid pitch method, '
                         f'valid methods are {PITCH_METHODS}')

    # oversample again (default factor of 4)
    post_zero_order_hold = zero_order_hold(pitched)

    # resample for output filter
    # TODO investigate the exception that arises when fortranarray cast is rm'd
    output = resample(np.asfortranarray(post_zero_order_hold),
                      TARGET_SAMPLE_RATE * ZERO_ORDER_HOLD_MULTIPLIER, OUTPUT_SAMPLE_RATE)

    if not skip_output_filter:
        # low pass equalization filter
        output = filter_output(output)

    af.write(output_file, output, OUTPUT_SAMPLE_RATE, '16bit', normalize)


if __name__ == '__main__':
    pitch()
