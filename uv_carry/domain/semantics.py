"""The image set of a carry and the meaning of each image.

The Blender adapter reports, for every image that the island's materials reach, how it is mapped and
how it feeds the shader: which input of the Principled BSDF, through which node. This module turns
those facts into one entry per image:

  * a semantic (colour, scalar data, packed channels, height, tangent-space or object-space normal,
    or not understood);
  * a status: "auto" (carried unless the user leaves it out), "optional" (mapped by the edited UV map
    but used through nodes UV Carry does not understand: carried only if the user includes it),
    "blocked" (moves with the island but cannot be carried as it is: the carry is refused while it is
    in the set, and the user may leave it out explicitly) or "unaffected" (mapped by another UV map or
    other coordinates: the move does not change how it maps, so it is never carried). UV Carry Lite
    carries no normal maps: an image used as one is blocked;
  * the alpha policy of its resampling and the diagnostics to show before the carry.

No image is left out silently: every image reached is listed with its status and reason.
"""

from dataclasses import dataclass, field

COLOR, DATA, PACKED, HEIGHT = "color", "data", "packed", "height"
NORMAL_TANGENT, NORMAL_OBJECT, UNKNOWN = "normal_tangent", "normal_object", "unknown"
NORMALS = frozenset({NORMAL_TANGENT, NORMAL_OBJECT})
NO_NORMALS = "UV Carry Lite does not carry normal maps"

COLOR_INPUTS = frozenset({"Base Color", "Specular Tint", "Coat Tint", "Sheen Tint", "Emission Color"})
NORMAL_INPUTS = frozenset({"Normal", "Coat Normal"})
ORDER = {COLOR: 0, DATA: 1, PACKED: 1, HEIGHT: 1, UNKNOWN: 2, NORMAL_OBJECT: 3, NORMAL_TANGENT: 3}
CARRIED_SOURCES = frozenset({"FILE", "GENERATED"})
NON_COLOR = frozenset({"Non-Color"})


@dataclass(frozen=True)
class Usage:
    """One way an image node feeds a material."""

    material: str
    route: str                  # "principled", "packed", "normal_map", "bump", "displacement" or "unknown"
    target: str                 # the Principled input reached, or "Displacement"
    output: str = "Color"       # the image node output used: "Color" or "Alpha"
    channel: str = ""           # packed: "Red", "Green" or "Blue"
    space: str = ""             # normal map: TANGENT, OBJECT, WORLD, ...
    detail: str = ""            # unknown: the node UV Carry does not understand


@dataclass(frozen=True)
class ImageFacts:
    """What the adapter knows about one image reached by the island's materials."""

    key: str
    width: int = 0
    height: int = 0
    channels: int = 4
    source: str = "FILE"
    has_data: bool = True
    colorspace: str = "sRGB"
    alpha_mode: str = "STRAIGHT"
    usages: tuple = ()          # Usage, for nodes mapped by the edited UV map
    unmapped: tuple = ()        # reasons why other nodes of this image do not follow the edited UV map
    blocked_mapping: tuple = () # reasons why a node follows the edited UV map but cannot be carried
    shared_with: tuple = ()     # other objects or materials that use the image


@dataclass(frozen=True)
class Entry:
    key: str
    semantic: str
    status: str                 # "auto", "optional", "blocked" or "unaffected"
    reason: str = ""            # why optional, blocked or unaffected
    roles: tuple = ()           # how it is used, e.g. ("Base Color", "Roughness (G)")
    alpha: str = "independent"  # resampling of the alpha channel: "straight", "premultiplied" or "independent"
    diagnostics: tuple = ()     # warnings shown before the carry
    width: int = 0
    height: int = 0
    channels: int = 4


@dataclass(frozen=True)
class ImageSet:
    entries: tuple              # Entry per image reached, in carry order
    targets: tuple              # keys of the entries the carry writes, in carry order
    blocking: tuple             # keys of the blocked entries still in the set

    def entry(self, key):
        return next((e for e in self.entries if e.key == key), None)

    @property
    def signature(self):
        """What must not change during a session: every entry's status, meaning and dimensions."""
        return tuple((e.key, e.semantic, e.status, e.reason, e.alpha, e.width, e.height, e.channels)
                     for e in self.entries)


def role(usage):
    if usage.route == "packed":
        return f"{usage.target} ({usage.channel[:1]})"
    if usage.route == "normal_map":
        return f"{usage.target} ({usage.space.lower()} normal map)"
    if usage.route in ("bump", "displacement"):
        return f"{usage.target} (height)"
    if usage.route == "unknown":
        return f"{usage.target} (through {usage.detail})"
    return usage.target + (" (alpha)" if usage.output == "Alpha" else "")


def kind(usage):
    """The semantic one usage gives its image."""
    if usage.route == "unknown":
        return UNKNOWN
    if usage.route == "packed":
        return PACKED
    if usage.route == "normal_map":
        return NORMAL_TANGENT if usage.space == "TANGENT" else NORMAL_OBJECT
    if usage.route in ("bump", "displacement"):
        return HEIGHT
    if usage.output == "Alpha" and usage.target == "Alpha":
        return None                                 # the alpha channel as opacity: compatible with any meaning
    if usage.target in NORMAL_INPUTS or usage.target == "Tangent":
        return UNKNOWN                              # a raw image into a vector input is not understood
    return COLOR if usage.target in COLOR_INPUTS else DATA


def classify(facts):
    """Entry for one image."""
    roles = tuple(dict.fromkeys(role(u) for u in facts.usages))
    kinds = {kind(u) for u in facts.usages} - {None}
    understood = kinds - {UNKNOWN}
    if not facts.usages:
        semantic = UNKNOWN
    elif not understood:
        semantic = UNKNOWN if kinds else DATA       # only the alpha channel is used: opacity data
    elif understood <= {DATA, HEIGHT}:
        semantic = HEIGHT if understood == {HEIGHT} else DATA
    elif len(understood) == 1:
        semantic = next(iter(understood))
    else:
        semantic = None
    alpha = alpha_policy(facts, semantic)
    common = dict(roles=roles, alpha=alpha, width=facts.width, height=facts.height, channels=facts.channels)

    if not facts.usages:
        return Entry(facts.key, UNKNOWN, "unaffected", "; ".join(facts.unmapped) or "not mapped by the edited UV map",
                     **common)
    if facts.source == "TILED":
        return Entry(facts.key, semantic or UNKNOWN, "blocked", "a UDIM image; UDIM is outside the MVP", **common)
    if facts.source not in CARRIED_SOURCES:
        return Entry(facts.key, semantic or UNKNOWN, "blocked",
                     f"a {facts.source.lower()} image; only still images are carried", **common)
    if not facts.has_data or not (facts.width and facts.height):
        return Entry(facts.key, semantic or UNKNOWN, "blocked", "the image has no pixels loaded", **common)
    if facts.blocked_mapping:
        return Entry(facts.key, semantic or UNKNOWN, "blocked", "; ".join(facts.blocked_mapping), **common)
    if semantic is None:
        uses = ", ".join(sorted(k.replace("_", " ") for k in understood))
        return Entry(facts.key, UNKNOWN, "blocked", f"used with incompatible meanings ({uses})", **common)
    if semantic in NORMALS:
        space = "tangent-space" if semantic == NORMAL_TANGENT else "object-space"
        return Entry(facts.key, semantic, "blocked", f"a {space} normal map: {NO_NORMALS}",
                     **common)

    notes = list(diagnostics(facts, semantic))
    if UNKNOWN in kinds and understood:
        notes.append("also used through nodes UV Carry does not understand: "
                     + ", ".join(sorted({u.detail for u in facts.usages if kind(u) == UNKNOWN})))
    if semantic == UNKNOWN:
        detail = ", ".join(sorted({u.detail for u in facts.usages if u.detail})) or "nodes UV Carry does not understand"
        return Entry(facts.key, UNKNOWN, "optional", f"used through {detail}; carried only if you include it",
                     diagnostics=tuple(notes), **common)
    return Entry(facts.key, semantic, "auto", diagnostics=tuple(notes), **common)


def alpha_policy(facts, semantic):
    """How the alpha channel takes part in resampling. A straight-alpha colour image is resampled with
    premultiplied colour, so transparent texels lend no colour to their neighbours (no halo); a
    premultiplied image is resampled as stored; channel-packed, opaque and data images channel by channel."""
    if facts.channels < 4 or facts.alpha_mode in ("CHANNEL_PACKED", "NONE"):
        return "independent"
    if semantic in (PACKED, DATA, HEIGHT, NORMAL_TANGENT, NORMAL_OBJECT):
        return "independent"
    return "premultiplied" if facts.alpha_mode == "PREMUL" else "straight"


def diagnostics(facts, semantic):
    if semantic == COLOR and facts.colorspace in NON_COLOR:
        yield "a colour image set to Non-Color"
    if semantic in (DATA, PACKED, HEIGHT, NORMAL_TANGENT, NORMAL_OBJECT) and facts.colorspace == "sRGB":
        yield "a data image read as sRGB colour"
    if facts.shared_with:
        yield "also used by " + ", ".join(facts.shared_with) + ": they change too"
    if facts.unmapped:
        yield "also reached through other coordinates (" + "; ".join(facts.unmapped) + "): they show the change too"


def resolve(facts_list, included=(), excluded=()):
    """The image set: every image reached, in carry order (colour, data, normals; then by name), and the
    images the carry writes. `included` names optional images the user added; `excluded` names images
    the user left out."""
    entries = sorted((classify(f) for f in facts_list), key=lambda e: (ORDER.get(e.semantic, 2), e.key))
    included, excluded = set(included), set(excluded)
    targets = tuple(e.key for e in entries
                    if (e.status == "auto" and e.key not in excluded) or (e.status == "optional" and e.key in included))
    blocking = tuple(e.key for e in entries if e.status == "blocked" and e.key not in excluded)
    return ImageSet(tuple(entries), targets, blocking)
