"""Geometry proposals must not silently become camera/session ground truth."""
import pytest

np = pytest.importorskip('numpy')
cv2 = pytest.importorskip('cv2')

from ml_tools.views import same_view


def features(offset=(0, 0), region=1):
    rng = np.random.default_rng(26)
    points = rng.uniform([50, 40], [590, 440], (200, 2)).astype(np.float32)
    points = (points-50)*region+50+np.array(offset, dtype=np.float32)
    descriptors = rng.integers(0, 256, (200, 32), dtype=np.uint8)
    return ((480, 640), points, descriptors)


def test_matching_static_scene_with_small_framing_shift_is_a_proposal():
    assert same_view(features(), features(offset=(5, 3)))


def test_same_room_from_substantially_changed_framing_stays_separate():
    assert not same_view(features(), features(offset=(100, 60)))


def test_shared_small_object_cannot_group_views():
    assert not same_view(features(region=0.1), features(region=0.1))
