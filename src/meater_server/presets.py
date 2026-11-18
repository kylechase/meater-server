"""Cook presets: what is being cooked, and the temperature that means "done".

Targets follow MEATER's own published guidance where it exists (medium-rare
beef 54-57 C, pork 63 C, chicken 74 C, salmon 52-54 C) and the usual US
doneness chart elsewhere, so the numbers line up with the phone app.

Two fields matter beyond the target:

``carryover_c``
    How far the centre keeps climbing after the meat leaves the heat.  The
    probe should come *out* at ``target_c - carryover_c``; that pull
    temperature is what the server actually counts down to.

``rest_minutes``
    How long the cut should rest before serving.  "Ready at" includes it.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field, replace


@dataclass(frozen=True, slots=True)
class Doneness:
    """One selectable doneness level within a preset."""

    id: str
    label: str
    target_c: float


@dataclass(frozen=True, slots=True)
class Preset:
    """A cut of meat, its doneness options, and its resting behaviour."""

    id: str
    label: str
    category: str
    donenesses: tuple[Doneness, ...]
    carryover_c: float = 2.0
    rest_minutes: float = 5.0
    note: str = ""

    def doneness(self, doneness_id: str | None) -> Doneness:
        """Look up a doneness by id, falling back to the default."""
        if doneness_id is not None:
            for d in self.donenesses:
                if d.id == doneness_id:
                    return d
        return self.default_doneness

    @property
    def default_doneness(self) -> Doneness:
        """The middle option, which is the usual choice for graded cuts."""
        return self.donenesses[len(self.donenesses) // 2]


def _steak_levels() -> tuple[Doneness, ...]:
    return (
        Doneness("rare", "Rare", 51.7),
        Doneness("medium_rare", "Medium rare", 57.2),
        Doneness("medium", "Medium", 62.8),
        Doneness("medium_well", "Medium well", 65.6),
        Doneness("well_done", "Well done", 71.1),
    )


def _single(label: str, target_c: float, ident: str = "done") -> tuple[Doneness, ...]:
    return (Doneness(ident, label, target_c),)


#: Shipped preset table.  Users add to or override this from config.
BUILTIN_PRESETS: tuple[Preset, ...] = (
    # --- Beef ---------------------------------------------------------------
    Preset("beef_steak", "Steak", "Beef", _steak_levels(), carryover_c=2.0, rest_minutes=5),
    Preset("beef_roast", "Roast / prime rib", "Beef", _steak_levels(),
           carryover_c=4.0, rest_minutes=15),
    Preset("beef_burger", "Burger", "Beef",
           (Doneness("medium_rare", "Medium rare", 57.2),
            Doneness("medium", "Medium", 62.8),
            Doneness("well_done", "Well done", 71.1)),
           carryover_c=2.0, rest_minutes=3,
           note="Ground beef is safest at 71 C / 160 F."),
    Preset("beef_brisket", "Brisket", "Beef", _single("Tender", 96.1),
           carryover_c=0.0, rest_minutes=60,
           note="Pull when it probes tender, anywhere from 93-99 C."),
    Preset("beef_short_ribs", "Short ribs", "Beef", _single("Tender", 95.0),
           carryover_c=0.0, rest_minutes=15),
    # --- Pork ---------------------------------------------------------------
    Preset("pork_chop", "Chops", "Pork",
           (Doneness("medium", "Medium", 62.8), Doneness("well_done", "Well done", 71.1)),
           carryover_c=3.0, rest_minutes=3),
    Preset("pork_tenderloin", "Tenderloin", "Pork",
           (Doneness("medium", "Medium", 62.8), Doneness("well_done", "Well done", 71.1)),
           carryover_c=3.0, rest_minutes=3),
    Preset("pork_loin_roast", "Loin roast", "Pork",
           (Doneness("medium", "Medium", 62.8), Doneness("well_done", "Well done", 71.1)),
           carryover_c=4.0, rest_minutes=10),
    Preset("pork_pulled", "Pulled pork / shoulder", "Pork", _single("Pullable", 95.0),
           carryover_c=0.0, rest_minutes=30),
    Preset("pork_ribs", "Ribs", "Pork", _single("Tender", 95.0), carryover_c=0.0, rest_minutes=10),
    Preset("pork_ham", "Ham (reheat)", "Pork", _single("Hot", 60.0),
           carryover_c=2.0, rest_minutes=10),
    # --- Lamb & veal --------------------------------------------------------
    Preset("lamb_chops", "Chops / rack", "Lamb", _steak_levels(), carryover_c=2.0, rest_minutes=5),
    Preset("lamb_leg", "Leg / shoulder", "Lamb", _steak_levels(), carryover_c=3.0, rest_minutes=15),
    Preset("veal_chop", "Chops / roast", "Veal", _steak_levels(), carryover_c=2.0, rest_minutes=10),
    # --- Poultry ------------------------------------------------------------
    Preset("chicken_breast", "Breast", "Chicken", _single("Cooked", 73.9),
           carryover_c=2.0, rest_minutes=5),
    Preset("chicken_thigh", "Thigh / dark meat", "Chicken", _single("Cooked", 79.4),
           carryover_c=2.0, rest_minutes=5),
    Preset("chicken_whole", "Whole bird", "Chicken", _single("Cooked", 73.9),
           carryover_c=3.0, rest_minutes=15,
           note="Probe the thickest part of the breast, clear of bone."),
    Preset("chicken_wings", "Wings", "Chicken", _single("Crisp", 82.2),
           carryover_c=1.0, rest_minutes=0),
    Preset("turkey_breast", "Breast", "Turkey", _single("Cooked", 73.9),
           carryover_c=3.0, rest_minutes=20),
    Preset("turkey_whole", "Whole bird", "Turkey", _single("Cooked", 73.9),
           carryover_c=3.0, rest_minutes=30),
    Preset("duck_breast", "Breast", "Duck",
           (Doneness("medium_rare", "Medium rare", 57.2),
            Doneness("medium", "Medium", 62.8),
            Doneness("well_done", "Well done", 73.9)),
           carryover_c=2.0, rest_minutes=5),
    # --- Fish ---------------------------------------------------------------
    Preset("fish_salmon", "Salmon", "Fish",
           (Doneness("rare", "Rare", 43.3),
            Doneness("medium_rare", "Medium rare", 48.9),
            Doneness("medium", "Medium", 52.8),
            Doneness("well_done", "Well done", 62.8)),
           carryover_c=1.0, rest_minutes=2),
    Preset("fish_tuna", "Tuna", "Fish",
           (Doneness("rare", "Rare", 43.3),
            Doneness("medium_rare", "Medium rare", 48.9),
            Doneness("medium", "Medium", 57.2)),
           carryover_c=1.0, rest_minutes=0),
    Preset("fish_white", "White fish (cod, halibut)", "Fish",
           (Doneness("medium", "Medium", 57.2), Doneness("well_done", "Well done", 62.8)),
           carryover_c=1.0, rest_minutes=2),
    Preset("fish_shrimp", "Shrimp", "Fish", _single("Cooked", 54.4),
           carryover_c=1.0, rest_minutes=0),
    # --- Game ---------------------------------------------------------------
    Preset("game_venison", "Venison", "Game",
           (Doneness("rare", "Rare", 51.7),
            Doneness("medium_rare", "Medium rare", 57.2),
            Doneness("medium", "Medium", 62.8)),
           carryover_c=2.0, rest_minutes=10),
    Preset("game_bison", "Bison", "Game", _steak_levels(), carryover_c=2.0, rest_minutes=5),
    # --- Manual -------------------------------------------------------------
    Preset("custom", "Custom target", "Other", _single("Custom", 62.8),
           carryover_c=0.0, rest_minutes=0,
           note="Set the target temperature by hand."),
)

CUSTOM_PRESET_ID = "custom"


@dataclass
class PresetLibrary:
    """Lookup over the builtin presets plus any user-defined ones."""

    presets: list[Preset] = field(default_factory=lambda: list(BUILTIN_PRESETS))

    def __post_init__(self) -> None:
        self._by_id = {p.id: p for p in self.presets}

    @classmethod
    def from_config(cls, overrides: list[dict] | None) -> PresetLibrary:
        """Build a library, applying user overrides by matching preset id.

        An override with an unknown id adds a new preset; a known id replaces
        only the fields it mentions.
        """
        presets = list(BUILTIN_PRESETS)
        index = {p.id: i for i, p in enumerate(presets)}
        for raw in overrides or []:
            parsed = _parse_preset(raw, presets, index)
            if parsed is None:
                continue
            pos, preset = parsed
            if pos is None:
                presets.append(preset)
                index[preset.id] = len(presets) - 1
            else:
                presets[pos] = preset
        return cls(presets)

    def get(self, preset_id: str | None) -> Preset | None:
        if preset_id is None:
            return None
        return self._by_id.get(preset_id)

    def __iter__(self):
        return iter(self.presets)

    def by_category(self) -> dict[str, list[Preset]]:
        grouped: dict[str, list[Preset]] = {}
        for preset in self.presets:
            grouped.setdefault(preset.category, []).append(preset)
        return grouped

    def option_label(self, preset: Preset, doneness: Doneness) -> str:
        """Human label used for the ESPHome select and the web UI."""
        if len(preset.donenesses) == 1:
            return f"{preset.category} - {preset.label}"
        return f"{preset.category} - {preset.label} - {doneness.label}"

    def select_options(self) -> list[tuple[str, str, str]]:
        """Every (label, preset_id, doneness_id) combination, in table order."""
        out: list[tuple[str, str, str]] = []
        for preset in self.presets:
            for doneness in preset.donenesses:
                out.append((self.option_label(preset, doneness), preset.id, doneness.id))
        return out


def _parse_preset(
    raw: dict, presets: list[Preset], index: dict[str, int]
) -> tuple[int | None, Preset] | None:
    """Turn one config table into a Preset, merged over an existing one."""
    preset_id = raw.get("id")
    if not isinstance(preset_id, str) or not preset_id:
        return None

    pos = index.get(preset_id)
    base = presets[pos] if pos is not None else None

    donenesses: tuple[Doneness, ...] | None = None
    if isinstance(raw.get("donenesses"), list):
        parsed = []
        for d in raw["donenesses"]:
            if not isinstance(d, dict):
                continue
            label = str(d.get("label", d.get("id", "Done")))
            ident = str(d.get("id", label.lower().replace(" ", "_")))
            try:
                target = float(d["target_c"])
            except (KeyError, TypeError, ValueError):
                continue
            parsed.append(Doneness(ident, label, target))
        if parsed:
            donenesses = tuple(parsed)
    elif "target_c" in raw:
        try:
            donenesses = _single(str(raw.get("doneness_label", "Done")), float(raw["target_c"]))
        except (TypeError, ValueError):
            donenesses = None

    if base is None:
        if donenesses is None:
            return None
        return None, Preset(
            id=preset_id,
            label=str(raw.get("label", preset_id)),
            category=str(raw.get("category", "Custom")),
            donenesses=donenesses,
            carryover_c=float(raw.get("carryover_c", 2.0)),
            rest_minutes=float(raw.get("rest_minutes", 5.0)),
            note=str(raw.get("note", "")),
        )

    changes: dict = {}
    if "label" in raw:
        changes["label"] = str(raw["label"])
    if "category" in raw:
        changes["category"] = str(raw["category"])
    if donenesses is not None:
        changes["donenesses"] = donenesses
    for key in ("carryover_c", "rest_minutes"):
        if key in raw:
            with contextlib.suppress(TypeError, ValueError):
                changes[key] = float(raw[key])
    if "note" in raw:
        changes["note"] = str(raw["note"])
    return pos, replace(base, **changes)
