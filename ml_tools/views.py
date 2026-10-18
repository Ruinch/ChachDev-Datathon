"""Propose visual-view groups locally; never claim camera/session ground truth.

Static scene features only: no faces, identities, audio, labels or model captions
are used for grouping. Review proposals locally before using them for a split.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import time
from pathlib import Path

from app.ml.video import decode_video, select_frames


def background_features(path: Path):
    import cv2
    import numpy as np
    from PIL import Image

    video = decode_video(path, fps=0.2, max_side=640, deadline=time.monotonic()+60)
    frames = select_frames(video, 0, video.duration, 7)
    images = [np.asarray(Image.open(io.BytesIO(f.jpeg)).convert('RGB')) for f in frames]
    median = np.median(np.stack(images), axis=0).astype(np.uint8)
    gray = cv2.cvtColor(median, cv2.COLOR_RGB2GRAY)
    orb = cv2.ORB_create(nfeatures=1800, fastThreshold=12)
    keypoints, descriptors = orb.detectAndCompute(gray, None)
    points = np.float32([p.pt for p in keypoints])
    return gray.shape, points, descriptors


def same_view(left, right) -> bool:
    import cv2
    import numpy as np

    shape_a, points_a, desc_a = left
    shape_b, points_b, desc_b = right
    if desc_a is None or desc_b is None or len(desc_a) < 50 or len(desc_b) < 50:
        return False
    matches = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(desc_a, desc_b, k=2)
    good = [pair[0] for pair in matches if len(pair) == 2 and pair[0].distance < 0.7 * pair[1].distance]
    if len(good) < 50:
        return False
    a = np.float32([points_a[m.queryIdx] for m in good])
    b = np.float32([points_b[m.trainIdx] for m in good])
    cv2.setRNGSeed(26)
    transform, mask = cv2.findHomography(a, b, cv2.RANSAC, 3.0)
    if transform is None or mask is None:
        return False
    # A homography can match the same room from a substantially different angle.
    # Require similar framing as well; otherwise this is a different view proposal.
    corners = np.float32([[0, 0], [shape_a[1], 0], [shape_a[1], shape_a[0]], [0, shape_a[0]]])
    projected = cv2.perspectiveTransform(corners.reshape(-1, 1, 2), transform).reshape(-1, 2)
    projected /= np.float32([shape_b[1], shape_b[0]])
    expected = np.float32([[0, 0], [1, 0], [1, 1], [0, 1]])
    if np.max(np.linalg.norm(projected - expected, axis=1)) > 0.08:
        return False
    inliers = mask.ravel().astype(bool)
    if inliers.sum() < 50 or inliers.mean() < 0.65:
        return False
    # A single logo, garment or small object must not group unrelated scenes.
    coverage = [cv2.contourArea(cv2.convexHull(points[inliers])) / (shape[0]*shape[1])
                for points, shape in [(a, shape_a), (b, shape_b)]]
    return min(coverage) >= 0.18


def propose(manifest: dict) -> dict:
    unique = {r['sha256']: r for r in manifest['records']}
    hashes = sorted(unique)
    features = {}
    for index, sha in enumerate(hashes):
        path = Path(unique[sha]['path'])
        with path.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != sha:
                raise ValueError('Source changed since manifest audit')
        features[sha] = background_features(path)
        print(json.dumps({'views_processed': index+1, 'unique_videos': len(hashes)}), flush=True)
    # Complete-link groups prevent a chain of small pans from joining distant views.
    groups = []
    for sha in hashes:
        for group in groups:
            if all(same_view(features[sha], features[other]) for other in group):
                group.append(sha)
                break
        else:
            groups.append([sha])
    names = {sha: f'view-{i+1:03}' for i, group in enumerate(groups) for sha in group}
    records = [{'id': r['id'], 'sha256': r['sha256'], 'view_group': names[r['sha256']],
                'human_verified': False} for r in manifest['records']]
    return {'version': 1, 'method': 'temporal_median_ORB_framing_complete_link_v2',
            'human_verified': False, 'purpose': 'diagnostic stratification and local review only',
            'limitations': 'Visual similarity is not camera/session identity. Distinct groups may still share a session. Do not finalize a holdout split from these proposals.',
            'summary': {'videos': len(records), 'unique_videos': len(hashes), 'proposed_views': len(groups)},
            'records': records}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest', type=Path, default=Path('.local_ml/dataset.json'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit('Choose a new output file to preserve previous view proposals')
    result = propose(json.loads(args.manifest.read_text(encoding='utf-8')))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(result['summary']))


if __name__ == '__main__':
    main()
