"""Unit tests for SceneIndex indexing and SceneDelta differential engine."""

from servo.perception.scene_index import SceneIndex, SceneElement, SceneDelta


def test_scene_index_indexing_and_lookup():
    el0 = SceneElement(
        element_id=0,
        label="File",
        role="MenuItem",
        bbox_screen=(10, 10, 50, 30),
        bbox_norm=(10, 10, 50, 30),
        center_screen=(30, 20),
    )
    el1 = SceneElement(
        element_id=1,
        label="Text editor",
        role="Edit",
        bbox_screen=(10, 40, 500, 400),
        bbox_norm=(10, 40, 500, 400),
        center_screen=(255, 220),
        value="Initial Text",
    )

    idx = SceneIndex(
        hwnd=2001,
        window_title="Untitled - Notepad",
        source="uia",
        elements={0: el0, 1: el1},
    )

    assert idx.get_by_id(0).label == "File"
    assert idx.get_by_id(999) is None

    matches = idx.find_by_label("editor")
    assert len(matches) == 1
    assert matches[0].element_id == 1


def test_scene_delta_computation():
    before = SceneIndex(
        hwnd=2001,
        window_title="Settings",
        source="uia",
        elements={
            0: SceneElement(
                element_id=0,
                label="Search",
                role="Edit",
                bbox_screen=(0, 0, 10, 10),
                bbox_norm=(0, 0, 10, 10),
                center_screen=(5, 5),
                value="",
            )
        },
    )

    after = SceneIndex(
        hwnd=2001,
        window_title="Settings - Results",
        source="uia",
        elements={
            0: SceneElement(
                element_id=0,
                label="Search",
                role="Edit",
                bbox_screen=(0, 0, 10, 10),
                bbox_norm=(0, 0, 10, 10),
                center_screen=(5, 5),
                value="Bluetooth",
            ),
            1: SceneElement(
                element_id=1,
                label="Bluetooth Devices",
                role="ListItem",
                bbox_screen=(0, 20, 100, 40),
                bbox_norm=(0, 20, 100, 40),
                center_screen=(50, 30),
            ),
        },
    )

    delta = SceneDelta.compute_diff(before, after)
    assert delta.has_changed is True
    assert delta.title_changed is True
    assert "Bluetooth Devices" in delta.added_labels
    assert "Search" in delta.value_changes
    assert delta.value_changes["Search"]["new_value"] == "Bluetooth"
