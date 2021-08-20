import subprocess
import os
import sys
from iocursor import Cursor
from pathlib import Path
from collections.abc import Mapping
from collections import defaultdict

import numpy as np
import pandas as pd

from dash_app.config import DashConfig
from data.person_detection import detect_person
from model.videopose3d import VideoPose3D
from data.video import Video
from data.video_dataset import VideoDataset
from data.h36m_skeleton_helper import H36mSkeletonHelper
from data.angle_helper import calc_common_angles
from data.gait_cycle_detector import GaitCycleDetector
from data.timeseries_utils import align_values, lp_filter, filter_outliers


# use ffprobe to get the duration of a video
def ffprobe_duration(filename):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                             "format=duration", "-of",
                             "default=noprint_wrappers=1:nokey=1", filename],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT)
    return float(result.stdout)

def get_duration(video_path):
    with Video(video_path) as video:
        return video.duration


def get_asset(file):
    return os.path.join(DashConfig.ASSETS_ROOT, file)


def random_upload_url(mkdir=False):
    from secrets import token_urlsafe
    url = Path(DashConfig.UPLOAD_ROOT) / token_urlsafe(16)
    if mkdir:
        url.mkdir(parents=True, exist_ok=True)
    return url

def _normed_cycles(angles, events):
    gcd = GaitCycleDetector()
    rhs, lhs, *_ = events
    r_normed_phases = gcd.normed_gait_phases(angles[:,0], rhs)
    l_normed_phases = gcd.normed_gait_phases(angles[:,1], lhs)
    return np.stack([r_normed_phases, l_normed_phases], axis=0)


def calc_metrics(angles, events):

    def _mean_std_str(values, unit): # expect 1d-array (N)
        return f"{values.mean():.1f} ± {values.std():.1f} {unit}"

    def _ratio_str(r, l):
        r, l= r.mean(), l.mean()
        sign = '+' if r/l > 1 else ''
        return sign + f"{(100 * r / l - 100):.2f}%"

    def _row(name, unit, right, left):
        right = filter_outliers(right)
        left = filter_outliers(left)
        return name, _mean_std_str(right, unit), \
               _mean_std_str(left, unit), _ratio_str(right, left)

    def _time(values, fps=50):
        return values / fps * 1000

    names = ['Metric', 'Right side', 'Left side', 'Right / Left ratio']

    rhs, lhs, rto, lto = events
    gcd = GaitCycleDetector()
    rcycles = gcd.normed_gait_phases(angles[:,0], rhs) # (N, 101)
    lcycles = gcd.normed_gait_phases(angles[:,1], lhs)

    rstance = align_values(rhs, rto, 'diff', tolerance=30, start_left=True)
    lstance = align_values(lhs, lto, 'diff', tolerance=30, start_left=True)
    
    rswing = align_values(rto, rhs, 'diff', tolerance=30, start_left=True)
    lswing = align_values(lto, lhs, 'diff', tolerance=30, start_left=True)

    rdouble = align_values(rhs, lto, 'diff', tolerance=10, start_left=True)
    ldouble = align_values(lhs, rto, 'diff', tolerance=10, start_left=True)


    metrics = [_row('Range of motion', '°', rcycles.ptp(axis=-1), lcycles.ptp(axis=-1)),
               _row('Max peak', '°', rcycles.max(axis=-1), lcycles.max(axis=-1)),
               _row('Max peak (loading response)', '°', rcycles[...,:30].max(axis=-1), lcycles[...,:30].max(axis=-1)),
               _row('Total step time', 'ms', _time(np.diff(rhs)), _time(np.diff(lhs))),
               _row('Stance time', 'ms', _time(rstance), _time(lstance)),
               _row('Swing time', 'ms', _time(rswing), _time(lswing)),
               _row('Double support time', 'ms', _time(rdouble), _time(ldouble)),
              ]
    return pd.DataFrame(metrics, columns=names)


def get_demo_data():
    demo_path = Path(DashConfig.DEMO_DATA) / 'demo_data.npz'
    demo_data = np.load(demo_path, allow_pickle=True)
    demo_pose = demo_data['pose_3d']
    demo_angles = 1.2 * np.stack([demo_data['rknee_angle'], demo_data['lknee_angle']], axis=-1)
    gait_events = (demo_data['rcycles'], demo_data['lcycles'], None, None)

    demo_pose = lp_filter(demo_pose, 7)
    demo_angles = lp_filter(demo_angles, 7)
    gait_events = GaitCycleDetector('h36m').detect(demo_pose, mode='auto')
    return demo_pose, demo_angles, gait_events


def avg_gait_phase(angles, events):
    gcd = GaitCycleDetector()
    rhs, lhs, _, _ = events
    r_normed_phases = gcd.normed_gait_phases(angles[:,0], rhs)
    l_normed_phases = gcd.normed_gait_phases(angles[:,1], lhs)
    r_mean = np.mean(r_normed_phases[:], axis=0)
    l_mean = np.mean(l_normed_phases[:], axis=0)
    return r_mean, l_mean


def get_norm_data(name='overground', joint=None, clinical=True):
    norm_path = Path(DashConfig.DEMO_DATA) / 'norm_data.npz'
    data = np.load(norm_path)
    keys = data['keys'].tolist() #index of key gives pos of joint
    norm_values = {}
    for i, k in enumerate(keys):
        norm_values[k] = data[name][:, i]
        if not clinical:
            norm_values[k] = 180 - norm_values[k]
    return norm_values

def get_sagital_view(pose_3d):
    RHip, LHip = 1, 4
    hip = pose_3d[0, RHip] - pose_3d[0, LHip]
    return dict(x=1+hip[0], y=2.5, z=0.25)


def memory_file(content):
    return Cursor(content) # alt: io.BytesIO(content)


def run_estimation_file(video_name='video.mp4', bbox_name='bboxes.npy', 
                    in_dir=None, video_range=None):
    if in_dir is None:
        in_dir = DashConfig.UPLOAD_ROOT
        
    in_dir = Path(in_dir)
    video_file = in_dir / video_name
    bbox_file = in_dir / bbox_name

    if bbox_file.exists():
        bboxes = np.load(bbox_file.resolve())
    else:
        bboxes = detect_person('yolov5s', video_file, bbox_file, 
                               video_out=in_dir / (video_file.stem + '_bboxes.mp4'))

    return run_estimation(video_file, bboxes, video_range)


def run_estimation(video_path, video_range=None, 
                    pipeline='Mediapipe + VideoPose3D',
                    detection='auto', ops=defaultdict):
    with Video(video_path) as video:

        start, end = map(lambda x: int(x*video.fps), video_range)
        end = min(end, len(video))
        video = video[start:end] if video_range is not None else video

        if pipeline == 'lpn': #'LPN + VideoPose3D':
            from model.lpn_estimator_2d import LPN_Estimator2D
            estimator_2d = LPN_Estimator2D()
            estimator_3d = VideoPose3D(normalized_skeleton=ops['skel_norm'])
            #gcd = GaitCycleDetector(pose_format='coco')
        elif pipeline == 'mp_nf': #'MediaPipe + VideoPose3D (w/o feet)':
            from model.mediapipe_estimator import MediaPipe_Estimator2D
            estimator_2d = MediaPipe_Estimator2D(out_format='coco')
            estimator_3d = VideoPose3D(normalized_skeleton=ops['skel_norm'])
            #gcd = GaitCycleDetector(pose_format='coco')
        elif pipeline == 'mp_wf': #'MediaPipe + VideoPose3D (w/ feet)':
            from model.mediapipe_estimator import MediaPipe_Estimator2D
            estimator_2d = MediaPipe_Estimator2D(out_format='openpose')
            estimator_3d = VideoPose3D(openpose=True, normalized_skeleton=ops['skel_norm'])
            #gcd = GaitCycleDetector(pose_format='openpose')
        else:
            raise ValueError('Invalid Pipeline: ', pipeline)
        gcd = GaitCycleDetector(pose_format='h36m')
        
        keypoints, meta = estimator_2d.estimate(video)
        pose_2d = keypoints['video']['custom'][0]
        pose_3d = estimator_3d.estimate(keypoints, meta)
        pose_3d = next(iter(pose_3d.values()))
        pose_3d = lp_filter(pose_3d, 6)

        angles = calc_common_angles(pose_3d, clinical=True)
        knee_angles = np.stack([angles['RKnee'], angles['LKnee']], axis=-1)
        if ops['debias']:
            knee_angles *= 1.2

        gait_events = gcd.detect(pose_3d, mode=detection)

        #skeleton_helper = H36mSkeletonHelper()
        #angles = skeleton_helper.pose2euler(pose_3d)
        #knee_angles = {k: v[:,1] for k, v in angles.items() if k.endswith('Knee')}

        return pose_3d, knee_angles, gait_events

