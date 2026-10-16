"""Trainable point classification: features, training, prediction, model files, hand labels."""
from __future__ import annotations

import json
import pickle
import zipfile

import numpy as np
import pytest

pytest.importorskip("sklearn")

from pyarchinit_photogrammetry.ai import classify, learn  # noqa: E402
from pyarchinit_photogrammetry.core import geometry  # noqa: E402
from pyarchinit_photogrammetry.core.pointcloud import PointCloud  # noqa: E402
from pyarchinit_photogrammetry.core.progress import Cancelled, Progress  # noqa: E402
from pyarchinit_photogrammetry.testing.synthetic import make_classified_cloud  # noqa: E402

SPACING = 0.025
CLASSES = [2, 3, 7, 64]  # ground, bushes, noise, wall


@pytest.fixture(scope="module")
def cloud():
    return make_classified_cloud(spacing=SPACING)


@pytest.fixture(scope="module")
def labelled(cloud):
    """3 % of the points of each class (at least 40), as a user would label them."""
    _, _, codes = cloud
    rng = np.random.default_rng(1)
    lab = np.zeros(len(codes), bool)
    for c in np.unique(codes):
        sel = np.nonzero(codes == c)[0]
        lab[rng.choice(sel, max(40, int(0.03 * len(sel))), replace=False)] = True
    return lab


@pytest.fixture(scope="module")
def model(cloud, labelled):
    xyz, rgb, codes = cloud
    return learn.train(xyz, np.where(labelled, codes, 0), rgb=rgb, random_state=0)


@pytest.fixture(scope="module")
def prediction(cloud, model):
    xyz, rgb, _ = cloud
    return learn.predict(model, xyz, rgb)


def test_features_describe_the_shapes(cloud):
    xyz, rgb, codes = cloud
    feats, names = learn.point_features(xyz, rgb)
    assert feats.shape == (len(xyz), len(names)) and feats.dtype == np.float32
    assert np.isfinite(feats).all() and len(set(names)) == len(names)
    voxel, scales = learn.suggest_scales(xyz)
    assert voxel == pytest.approx(SPACING, rel=0.2)  # sqrt of the area per point
    assert scales == pytest.approx([5 * voxel, 14 * voxel, 40 * voxel], rel=0.06)
    col = {n: feats[:, i] for i, n in enumerate(names)}
    r0 = f"{scales[0]:g}m"
    wall_faces = (codes == 64) & (col["height_above_ground"] < 0.6)
    assert np.median(col[f"verticality@{r0}"][wall_faces]) > 0.8
    assert np.median(col[f"verticality@{r0}"][codes == 2]) < 0.2
    assert np.median(col[f"planarity@{r0}"][codes == 2]) > 0.5
    assert np.median(col["excess_green"][codes == 3]) > np.median(col["excess_green"][codes == 2]) + 0.1
    assert np.median(col["height_above_ground"][codes == 2]) == pytest.approx(0, abs=0.02)
    assert np.median(col["height_above_ground"][(codes == 64) & (col[f"verticality@{r0}"] < 0.2)]) > 0.6
    assert np.median(col[f"density@{r0}"][codes == 7]) < 0.2 * np.median(col[f"density@{r0}"][codes == 2])


def test_classifier_learns_from_few_labels(cloud, labelled, model, prediction):
    _, _, codes = cloud
    pred, conf = prediction
    assert model.classes == CLASSES
    assert model.class_names[2] == "Ground" and model.class_names[64].startswith("Struttura muraria")
    assert len(model.scales) == 3 and model.feature_names == model.config.names
    assert model.oob_score > 0.95 and model.training_accuracy > 0.95
    assert model.confusion_matrix.shape == (4, 4) and model.confusion_matrix.sum() == model.stats["n_samples"]
    rest = ~labelled
    assert (pred[rest] == codes[rest]).mean() > 0.97
    for c in CLASSES:
        assert (pred[rest & (codes == c)] == c).mean() > 0.9, c
    assert pred.dtype == np.uint8 and conf.dtype == np.float32
    assert ((conf > 0) & (conf <= 1)).all() and np.median(conf) > 0.9
    assert "out-of-bag accuracy" in model.summary()


def test_low_confidence_points_are_unclassified(cloud, model, prediction):
    xyz, rgb, _ = cloud
    pred, conf = prediction
    thr = float(np.quantile(conf, 0.02)) + 1e-6
    low, low_conf = learn.predict(model, xyz, rgb, min_confidence=thr)
    assert np.array_equal(low_conf, conf)
    assert np.array_equal(low, np.where(conf < thr, learn.UNCLASSIFIED, pred))
    assert (low == learn.UNCLASSIFIED).sum() > 0


def test_big_clouds_use_a_coarser_core(cloud, labelled, model):
    xyz, rgb, codes = cloud
    pred, _ = learn.predict(model, xyz, rgb, max_core_points=10_000)
    assert len(pred) == len(xyz)
    assert (pred[~labelled] == codes[~labelled]).mean() > 0.9


def test_model_file_round_trip(cloud, model, prediction, tmp_path):
    xyz, rgb, _ = cloud
    path = model.save(tmp_path / f"scavo{learn.MODEL_SUFFIX}")
    with zipfile.ZipFile(path) as zf:
        assert sorted(zf.namelist()) == ["estimator.pkl", "model.json"]
    info = learn.read_model_info(path)
    assert info["format"] == learn.MODEL_FORMAT and info["format_version"] == learn.MODEL_VERSION
    assert [c["code"] for c in info["classes"]] == CLASSES
    assert info["feature_names"] == model.feature_names and info["features"]["scales"] == model.scales
    assert info["info"]["sklearn_version"] and info["stats"]["validation"] == "out-of-bag"
    loaded = learn.ClassifierModel.load(path)
    assert loaded.class_names == model.class_names and loaded.voxel == model.voxel
    assert loaded.stats == json.loads(json.dumps(model.stats))
    pred, conf = loaded.predict(xyz, rgb)
    assert np.array_equal(pred, prediction[0]) and np.array_equal(conf, prediction[1])


def test_model_file_cannot_run_code(model, tmp_path):
    class Exploit:
        def __reduce__(self):
            import os

            return os.system, (f"touch {tmp_path / 'owned'}",)

    def call(module, name, arg):  # protocol 4 pickle of module.name(arg)
        text = [s.encode() for s in (module, name, arg)]
        return b"\x80\x04" + b"".join(b"\x8c" + bytes([len(t)]) + t for t in text[:2]) + b"\x93" \
            + b"\x8c" + bytes([len(text[2])]) + text[2] + b"\x85R."

    path = model.save(tmp_path / "good.ppgclf")
    payloads = [pickle.dumps(Exploit()), call("os", "system", "true"),
                call("sklearn._config", "os.system", "true"),  # dotted names reach other modules
                call("sklearn.ensemble", "_forest.RandomForestClassifier", "x")]
    for payload in payloads:
        bad = tmp_path / "bad.ppgclf"
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(bad, "w") as dst:
            dst.writestr("model.json", src.read("model.json"))
            dst.writestr("estimator.pkl", payload)
        with pytest.raises(ValueError, match="not allowed"):
            learn.ClassifierModel.load(bad)
    assert not (tmp_path / "owned").exists()
    (tmp_path / "notes.ppgclf").write_text("not a model")
    with pytest.raises(ValueError, match="not a classifier file"):
        learn.read_model_info(tmp_path / "notes.ppgclf")


def test_training_is_deterministic(cloud, labelled, model):
    xyz, rgb, codes = cloud
    again = learn.train(xyz, np.where(labelled, codes, 0), rgb=rgb, random_state=0)
    assert pickle.dumps(again.estimator) == pickle.dumps(model.estimator)
    other = learn.train(xyz, np.where(labelled, codes, 0), rgb=rgb, random_state=0, method="gradient_boosting")
    assert other.method == "gradient_boosting" and other.validation_accuracy > 0.95


def test_training_needs_two_classes_and_matching_inputs(cloud, model):
    xyz, rgb, codes = cloud
    with pytest.raises(ValueError, match="two classes"):
        learn.train(xyz, np.where(codes == 2, 2, 0), rgb=rgb)
    with pytest.raises(ValueError, match="colour"):
        learn.predict(model, xyz)


def test_cancel_stops_prediction(cloud, model):
    xyz, rgb, _ = cloud

    def cb(fraction, message):
        if fraction > 0.1:  # while the neighbourhoods are prepared
            progress.cancel()

    progress = Progress(cb)
    with pytest.raises(Cancelled):
        learn.predict(model, xyz, rgb, progress=progress)


def test_height_from_given_ground(cloud):
    xyz, rgb, codes = cloud
    ground = np.full(len(xyz), xyz[:, 2].min())
    feats, names = learn.point_features(xyz, None, scales=[0.12, 0.3, 0.8], ground=ground)
    hag = feats[:, names.index("height_above_ground")]
    assert hag.min() >= 0 and np.median(hag[codes == 64]) > np.median(hag[codes == 2]) + 0.3
    assert "red" not in names and names[0] == "linearity@0.12m"
    nohag, names2 = learn.point_features(xyz[:2000], None, scales=[0.12, 0.3, 0.8], ground=False)
    assert "height_above_ground" not in names2 and nohag.shape == (2000, 39)


def test_sample_dtm(tmp_path):
    rasterio = pytest.importorskip("rasterio")
    from rasterio.transform import from_origin

    path = tmp_path / "dtm.tif"
    data = np.arange(12, dtype=np.float32).reshape(3, 4)
    with rasterio.open(path, "w", driver="GTiff", width=4, height=3, count=1, dtype="float32",
                       transform=from_origin(100.0, 50.0, 1.0, 1.0), nodata=-9999) as dst:
        dst.write(data, 1)
    z = learn._sample_raster(path, np.array([100.5, 103.5, 99.0]), np.array([49.5, 47.5, 49.0]))
    assert z[0] == 0 and z[1] == 11 and np.isnan(z[2])


# ---------------------------------------------------------------- chunks and command line
def _project(cloud, path):
    """A saved project whose chunk holds the cloud in an internal frame (rotated, scaled, shifted)."""
    import pyarchinit_photogrammetry as ppg

    xyz, rgb, codes = cloud
    doc = ppg.Document()
    ch = doc.add_chunk("Saggio 1")
    ch.crs = "EPSG:3004"
    a = np.radians(30)
    rot = np.array([[np.cos(a), -np.sin(a), 0], [np.sin(a), np.cos(a), 0], [0, 0, 1]])
    ch.transform = geometry.compose_similarity(2.0, rot, xyz.mean(0))
    doc.save(path)
    ch.point_cloud = PointCloud(ch.to_internal(xyz), rgb)
    return doc, ch


def test_hand_labels_train_and_survive_classification(cloud, labelled, tmp_path):
    _, _, codes = cloud
    doc, ch = _project(cloud, tmp_path / "scavo.ppg")
    for c in CLASSES:
        assert classify.set_class(ch, labelled & (codes == c), c) == int((labelled & (codes == c)).sum())
    pc = ch.point_cloud
    assert np.array_equal(pc.manual, labelled) and (pc.classification[labelled] == codes[labelled]).all()
    model = learn.train_on_chunk(ch)
    assert model.classes == CLASSES and model.info["trained_on"]["chunk"] == "Saggio 1"
    assert model.voxel == pytest.approx(SPACING, rel=0.2)  # world metres, not internal units
    # a hand label the classifier does not know: it must stay as it is
    odd = np.nonzero(labelled & (codes == 2))[0][:25]
    classify.set_class(ch, odd, 70, label="Roccia")
    pc, conf = learn.classify_chunk_with_model(ch, model)
    stored = ch.point_cloud
    assert np.array_equal(stored.classification, pc.classification) and np.array_equal(stored.manual, labelled)
    assert (stored.classification[odd] == 70).all() and (conf[labelled] == 1).all()
    rest = ~labelled
    assert (stored.classification[rest] == codes[rest]).mean() > 0.97
    assert ch.meta["classes"]["70"] == "Roccia" and ch.meta["classes"]["3"] == "Low vegetation"
    # the rule-based classification keeps them too
    ruled = classify.classify_chunk(ch)
    assert (ruled.classification[odd] == 70).all() and np.array_equal(ruled.manual, labelled)
    # unless asked: then the flags stay only where the classes agree
    pc, _ = learn.classify_chunk_with_model(ch, model, overwrite_manual=True)
    assert (pc.classification[odd] == 2).all() and not pc.manual[odd].any()
    assert pc.manual.sum() > 0.9 * (labelled.sum() - len(odd))
    assert classify.clear_manual(ch) == int(pc.manual.sum()) and not ch.point_cloud.manual.any()


def test_cli_train_and_apply(cloud, labelled, tmp_path):
    from typer.testing import CliRunner

    from pyarchinit_photogrammetry.cli import app

    _, _, codes = cloud
    doc, ch = _project(cloud, tmp_path / "scavo.ppg")
    for c in CLASSES:
        classify.set_class(ch, labelled & (codes == c), c)
    doc.save()
    model = tmp_path / "scavo.ppgclf"
    runner = CliRunner()
    res = runner.invoke(app, ["classify-train", str(tmp_path / "scavo.ppg"), str(model), "--trees", "30"])
    assert res.exit_code == 0, res.output
    assert "out-of-bag accuracy" in res.output and model.exists()
    res = runner.invoke(app, ["classify-apply", str(tmp_path / "scavo.ppg"), str(model), "--min-confidence", "0.5"])
    assert res.exit_code == 0, res.output
    stats = json.loads(res.output.strip().splitlines()[-1])
    assert set(stats) <= {"1", "2", "3", "7", "64"} and stats["2"] > 0.5 * len(codes)
