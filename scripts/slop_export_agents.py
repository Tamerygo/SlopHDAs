"""SLOP — export animated Houdini crowd agents as FBX for the platform.

    Standalone Local Orchestration Platform — PRO, 3D Asset Edition
    Houdini-side companion tool.  MIT, see LICENSE in this repository.

WHAT IT DOES
------------
Select the node whose geometry carries the agents — the crowd output, e.g.
`OUTCrowdUNREAL` — and run this. For **every agent in that geometry** it writes
one animated FBX (skin + rest skeleton + animated skeleton) into the platform's
output folder, and copies the agent's colour map beside it under the name the
platform's Unreal bridge looks for.

Then switch to the platform, open **Library**, queue the FBX files you want and
press **Unreal** (or **Houdini**). One press sends the whole queue.

WHY IT NO LONGER CALLS THE PLATFORM ITSELF
------------------------------------------
The previous version shelled out to `venv\\Scripts\\python.exe` running
`core/unreal_link.py`, and decided whether it had worked by looking for an
emoji in the output. Three things are wrong with that, and the third is fatal:

  1. It hard-coded the internals of another program. Move a folder, rename a
     module, and a script in Houdini stops working with no clue why.
  2. "Did it work?" was answered by searching stdout for `🔥 Siker`. A reworded
     log line is then indistinguishable from a failure.
  3. **A released build has no `python.exe` and no `core/unreal_link.py`.** It
     is a compiled binary. So that call could never work for anyone but the
     author, on the one machine where the source tree happens to sit.

Exporting files and letting the platform send them has none of those problems,
and gains the queue: many agents, one press.

CONFIGURE
---------
Nothing, in the normal case. Both settings that used to need an absolute path —
where the files go and how the platform is run — are discovered from the
platform's own `workspace/exports/platform.json`, which it writes at every
start. Set them at the top of this file only to override that.
"""
import json
import os
import shutil
import traceback

import hou

# --------------------------------------------------------------------------
# CONFIGURATION
# --------------------------------------------------------------------------
# Where the FBX files are written.
#
# Leave it EMPTY and the script finds the platform's own export folder: it sits
# beside this script when the platform installed it, and the platform writes a
# `platform.json` inside it at every start. That folder is deliberately NOT the
# Library's — a two-hundred-agent crowd is two hundred FBX files, and under
# `workspace\outputs` every one of them becomes a card and buries the renders
# you actually made. Files here reach Unreal through SEND_COMMAND below.
#
# Set it only to override that. Pointing it at
#   <install folder>\pro\workspace\outputs\Houdini_Exports
# puts the exports in the Library instead, which is reasonable for a single
# character you want to queue by hand.
OUTPUT_DIR = ""

# Which animation clip to bake. "" means: use the first clip the agent offers,
# and say which one that was. Set it explicitly (e.g. "Dance") to pin it.
CLIP_NAME = ""

# The name the clip is given *inside* the FBX. "" copies CLIP_NAME. These are
# two different things and the old script set them to two different values
# ("Dance" in, "SlopAnim" out) without saying so.
EXPORT_CLIP_NAME = ""

# Where the colour map hangs, relative to the selected node's parent. The first
# one that exists and has a `file` parameter wins.
TEXTURE_NODE_PATHS = (
    "matnet1/mtlx_diffuse",
    "matnet1/diffuse",
    "shopnet1/mtlx_diffuse",
)

# Export every agent, or only the first one. Leave True — the whole point.
EXPORT_ALL_AGENTS = True

# How the files reach Unreal.
#
# Leave this EMPTY and the script finds the platform by itself: the platform
# writes `workspace\exports\platform.json` every time it starts, saying how to
# run it on *this* machine — the venv's interpreter and app.py in a source
# install, one executable in a released build. Neither can be written down here
# in advance, because both differ per computer, and a script that has to be
# edited with the right two paths works on exactly one of them.
#
# Set it only to override that:
#   SEND_COMMAND = [r"D:\...\AI_SLOP_PRO.exe"]
#
# Note what this is NOT: it does not reach inside the program. It calls the
# program's own `--send-unreal` command line and reads the **exit code**. The
# version of this script that came before searched the output for the text
# "🔥 Siker", so a reworded log line was indistinguishable from a failure.
SEND_COMMAND = []

# Frames. None means the playbar's current playback range.
FRAME_RANGE = None


# --------------------------------------------------------------------------
# Small helpers — every one of them exists because a Houdini version changed
# something underneath a script like this one.
# --------------------------------------------------------------------------
def _log(message):
    print("[SLOP] {}".format(message))


def _tell(message, error=False, copyable=False):
    """Say it in the console always, and in a dialog when there is a screen.

    `copyable` puts the text in a field you can select rather than in a label
    you can only read. Houdini's `displayMessage` cannot be copied from, and a
    message you cannot copy is a message you have to retype into a bug report —
    which is how a `UnicodeEncodeError` reached me as a screenshot.

    Anything that reports a failure is copyable, always. That is the whole
    point: the summary of a good run is three lines you do not need, and the
    one line of a bad run is the one you do.
    """
    _log(message.replace("\n", " | "))
    try:
        if not hou.isUIAvailable():
            return
        if copyable or error:
            hou.ui.readMultiInput(
                "Export finished. This text is selectable — Ctrl+A, Ctrl+C.",
                ["Log:"], initial_contents=[message], buttons=("OK",),
                severity=hou.severityType.Error if error else hou.severityType.Message,
                title="SLOP — export agents")
        else:
            hou.ui.displayMessage(
                message, severity=hou.severityType.Message,
                title="SLOP — export agents")
    except Exception:
        pass


def _set(node, name, value):
    """Set a parameter if this build of Houdini has it.

    A missing parameter raised and took the whole export with it. Now it is
    reported and the export continues — the FBX may be imperfect, and that is
    strictly better than no FBX and a stack trace.
    """
    parm = node.parm(name)
    if parm is None:
        _log("note: {} has no parameter '{}' in this Houdini build — skipped"
             .format(node.type().name(), name))
        return False
    try:
        parm.set(value)
        return True
    except Exception as exc:
        _log("note: could not set {}.{} = {!r} ({})".format(
            node.type().name(), name, value, exc))
        return False


def _set_menu(node, name, wanted):
    """Choose a menu entry by *meaning* rather than by index.

    `blast`'s group type is an ordered menu, and "primitives is index 4" is a
    fact about one version of Houdini, not about Houdini. This looks the entry
    up by its token or its label, so the script survives the menu growing an
    item — and says so when it cannot find one.
    """
    parm = node.parm(name)
    if parm is None:
        _log("note: {} has no parameter '{}'".format(node.type().name(), name))
        return False
    try:
        tokens = list(parm.menuItems())
        labels = list(parm.menuLabels())
    except Exception:
        tokens, labels = [], []
    for index, token in enumerate(tokens):
        label = labels[index] if index < len(labels) else ""
        if wanted.lower() in token.lower() or wanted.lower() in label.lower():
            try:
                parm.set(token)
            except Exception:
                parm.set(index)
            return True
    _log("note: no '{}' entry in {}.{} (offers: {})".format(
        wanted, node.type().name(), name, ", ".join(tokens) or "nothing"))
    return False


def _frame_range():
    if FRAME_RANGE:
        return float(FRAME_RANGE[0]), float(FRAME_RANGE[1])
    for reader in (getattr(hou.playbar, "playbackRange", None),
                   getattr(hou.playbar, "frameRange", None)):
        if reader is None:
            continue
        try:
            start, end = reader()
            return float(start), float(end)
        except Exception:
            continue
    return 1.0, 100.0


def _safe_name(text):
    keep = "".join(ch if (ch.isalnum() or ch in "-_") else "_" for ch in str(text))
    return keep.strip("_") or "agent"


def _resolve_output_dir():
    """The platform's export folder, found rather than typed.

    An absolute path written into a script works on the machine of whoever
    typed it and nowhere else. This one is discovered: the platform installs
    this script into its own `workspace/hda`, and writes `platform.json` into
    `workspace/exports` every time it starts — so walking up from where this
    file sits finds both the folder and the way to run the platform.

    Returns "" when it cannot be found, and the caller says so rather than
    inventing a directory on somebody's D: drive.
    """
    if OUTPUT_DIR:
        return OUTPUT_DIR
    starts = []
    try:
        starts.append(os.path.dirname(os.path.abspath(__file__)))
    except Exception:
        pass
    try:
        current = hou.hipFile.path()
        if current:
            starts.append(os.path.dirname(os.path.abspath(current)))
    except Exception:
        pass
    for start in starts:
        folder = start
        for _ in range(8):
            candidate = os.path.join(folder, "exports")
            if os.path.isfile(os.path.join(candidate, "platform.json")):
                found = os.path.join(candidate, "Houdini")
                _log("export folder found beside the platform: {}".format(found))
                return found
            parent = os.path.dirname(folder)
            if parent == folder:
                break
            folder = parent
    return ""


def _agent_labels(geometry):
    """One label per agent primitive, and the reason it is per *primitive*.

    A packed agent is one primitive, so the primitive count is the agent count
    and needs no API that might not be there. A name comes from whichever of
    the usual attributes this setup happens to carry; when none does, the index
    is the name. Nothing here guesses at a crowd's structure.
    """
    labels = []
    attribute = None
    for candidate in ("name", "agentname", "agentdefinition", "crowd_name"):
        if geometry.findPrimAttrib(candidate) is not None:
            attribute = candidate
            break
    for index, prim in enumerate(geometry.prims()):
        label = ""
        if attribute:
            try:
                label = str(prim.attribValue(attribute) or "")
            except Exception:
                label = ""
        labels.append("{:02d}_{}".format(index, _safe_name(label)) if label
                      else "{:02d}".format(index))
    if attribute:
        _log("agent names read from the '{}' primitive attribute".format(attribute))
    else:
        _log("no agent-name attribute found — agents are numbered by primitive")
    return labels


def _copy_texture(parent_net, destination_stem):
    """Put the colour map beside the FBX, under the name the platform reads.

    The platform's Unreal bridge looks for an image next to the mesh whose stem
    starts with the mesh's own, skipping normal/roughness-style tags — so
    `<fbx stem>_diffuse.png` is found without any configuration at the other
    end. Returns the path written, or "".
    """
    for relative in TEXTURE_NODE_PATHS:
        node = parent_net.node(relative)
        if node is None or node.parm("file") is None:
            continue
        try:
            source = node.parm("file").eval()
        except Exception:
            continue
        if not source or not os.path.exists(source):
            continue
        extension = os.path.splitext(source)[1] or ".png"
        target = os.path.join(
            OUTPUT_DIR, "{}_diffuse{}".format(destination_stem, extension))
        shutil.copy2(source, target)
        _log("texture copied: {}".format(target))
        return target.replace("\\", "/")
    _log("no colour map found under {} — the import will be untextured, and "
         "the platform will say so".format(" / ".join(TEXTURE_NODE_PATHS)))
    return ""


def _agent_transform(geometry, prim_index):
    """Where this agent stands, in centimetres, if Houdini will tell us.

    Only ever used as *information* in the sidecar: the exported geometry is
    already in world space, because unpacking a packed agent applies its
    transform. Reading it is still worth doing — it is what a later version
    needs in order to export one shared mesh and place N actors instead of
    exporting N copies.
    """
    try:
        prim = geometry.prims()[prim_index]
    except Exception:
        return None
    for reader in ("fullTransform", "transform"):
        method = getattr(prim, reader, None)
        if method is None:
            continue
        try:
            matrix = method()
            translate = matrix.extractTranslates()
            rotate = matrix.extractRotates()
            scale = matrix.extractScales()
            return {"translate": [float(v) for v in translate],
                    "rotate": [float(v) for v in rotate],
                    "scale": [float(v) for v in scale]}
        except Exception:
            continue
    return None


def _write_sidecar(stem, prim_index, label, clip, transform, crowd):
    """Tell the platform what this FBX is. Written for every export, not only
    for crowds, because it answers two different questions.

    **Is it a character?** Unreal's `FbxImportUI` has
    `automated_import_should_detect_type`, which defaults to True and overrules
    `import_as_skeletal` — a rigged character then arrives as a static mesh, a
    statue with no skeleton and no animation. The bridge turns that detection
    off, but only for a file this sidecar vouches for; guessing on somebody
    else's FBX would break an import that already worked.

    **Is it one of a crowd?** If so the Unreal bridge must not lay it out on
    the display grid, which is right for one barrel and one sword and destroys
    the only thing that made a crowd a crowd.
    """
    payload = {
        "kind": "dcc_character",
        "animated": True,
        "crowd": bool(crowd),
        "placement": "baked",
        "agent_index": int(prim_index),
        "agent_label": str(label),
        "clip": str(clip or ""),
        "written_by": "slop_export_agents.py",
    }
    if transform:
        payload.update(transform)
    path = os.path.join(OUTPUT_DIR, "{}.crowd.json".format(stem))
    try:
        with open(path, "w") as handle:
            json.dump(payload, handle, indent=2)
        return path.replace("\\", "/")
    except Exception as exc:
        _log("note: could not write {} ({})".format(path, exc))
        return ""


# --------------------------------------------------------------------------
# One agent
# --------------------------------------------------------------------------
def _export_one(parent_net, source_node, prim_index, label, asset_stem):
    """Write one agent's FBX. Returns (path, clip_used) or (None, reason)."""
    fbx_path = os.path.join(OUTPUT_DIR, "{}.fbx".format(asset_stem)).replace("\\", "/")
    temporary = []
    try:
        # 1 — keep this agent and nothing else. A packed agent is one
        #     primitive, so isolating one is a Blast and not an API question.
        isolate = parent_net.createNode("blast", "TEMP_SLOP_ISOLATE")
        temporary.append(isolate)
        isolate.setInput(0, source_node)
        _set(isolate, "group", str(prim_index))
        _set_menu(isolate, "grouptype", "prim")
        _set(isolate, "negate", 1)          # delete everything NOT selected

        # 2 — unpack the agent into skin, rest skeleton and animated skeleton.
        unpack = parent_net.createNode("kinefx::agentcharacterunpack", "TEMP_SLOP_UNPACK")
        temporary.append(unpack)
        unpack.setInput(0, isolate)
        _set(unpack, "geo_transferattributes", "v")
        _set(unpack, "output", "agentclippose")
        _set(unpack, "skel_transferattributes", "transform")

        clip = CLIP_NAME
        parm = unpack.parm("agentclipname")
        available = []
        if parm is not None:
            try:
                available = [item for item in parm.menuItems() if item]
            except Exception:
                available = []
        if not clip:
            clip = available[0] if available else ""
        if clip:
            _set(unpack, "agentclipname", clip)
        if available:
            _log("agent {} clips: {} → using '{}'".format(
                label, ", ".join(available), clip or "(default)"))

        # 3 — the KineFX character ROP, and its three wires.
        rop = parent_net.createNode("kinefx::rop_fbxcharacteroutput",
                                    "TEMP_SLOP_EXPORT")
        temporary.append(rop)
        rop.setInput(0, unpack, 0)          # skin
        rop.setInput(1, unpack, 1)          # rest skeleton
        rop.setInput(2, unpack, 2)          # animated skeleton

        _set(rop, "outputfilepath", fbx_path)
        _set(rop, "cliprangemode", "normal")
        _set(rop, "clipname", EXPORT_CLIP_NAME or clip or "SlopAnim")
        _set(rop, "axissystem", "currentup")
        _set(rop, "convertaxis", 1)
        _set(rop, "outputunit", "cm")
        _set(rop, "computesmoothinggroups", 1)
        _set(rop, "removejointscaling", 1)

        start, end = _frame_range()
        if rop.parmTuple("f"):
            rop.parmTuple("f").set((start, end, 1))

        _log("exporting agent {} → {} (frames {:g}–{:g})".format(
            label, fbx_path, start, end))
        rop.parm("execute").pressButton()

        if not os.path.exists(fbx_path):
            return None, "the ROP ran but wrote no file"
        return fbx_path, clip
    except Exception as exc:
        return None, "{}: {}".format(type(exc).__name__, exc)
    finally:
        # Always, including on the way out of an exception — a failed export
        # used to leave TEMP_ nodes behind in the network.
        for node in reversed(temporary):
            try:
                node.destroy()
            except Exception:
                pass


# --------------------------------------------------------------------------
# Every agent
# --------------------------------------------------------------------------
def export_agents():
    selected = hou.selectedNodes()
    if not selected:
        _tell("Select the crowd output node first (for example OUTCrowdUNREAL), "
              "then run this again.", error=True)
        return []

    source_node = selected[0]
    parent_net = source_node.parent()
    if len(selected) > 1:
        _log("{} nodes selected — using '{}'".format(len(selected), source_node.name()))

    try:
        geometry = source_node.geometry()
    except Exception as exc:
        _tell("'{}' has no geometry to read ({}). Select a SOP.".format(
            source_node.name(), exc), error=True)
        return []
    if geometry is None:
        _tell("'{}' produced no geometry. Cook it once, then run this again."
              .format(source_node.name()), error=True)
        return []

    count = len(geometry.prims())
    if count == 0:
        _tell("'{}' contains no primitives, so there is no agent to export."
              .format(source_node.name()), error=True)
        return []

    global OUTPUT_DIR
    OUTPUT_DIR = _resolve_output_dir()
    if not OUTPUT_DIR:
        _tell("I could not find the platform's export folder from here, and "
              "OUTPUT_DIR at the top of this script is empty.\n\n"
              "Start the platform once — it writes workspace\\exports\\platform.json "
              "at every start — or set OUTPUT_DIR yourself to the folder you "
              "want the FBX files written to.", error=True, copyable=True)
        return []
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    labels = _agent_labels(geometry)
    indices = list(range(count)) if EXPORT_ALL_AGENTS else [0]
    _log("{} agent(s) found in '{}'; exporting {}".format(
        count, source_node.name(), len(indices)))

    base = _safe_name(parent_net.name())
    written, failed = [], []
    for index in indices:
        label = labels[index]
        stem = "{}_{}".format(base, label) if count > 1 else "{}_Animated".format(base)
        # On success the second value is the clip that was baked; on failure
        # it is why. One name for two meanings would be a bug waiting.
        path, clip_or_reason = _export_one(parent_net, source_node, index, label, stem)
        if path:
            _copy_texture(parent_net, stem)
            # Always written. It says "this is a rigged, animated character",
            # which is what makes Unreal import it as one, and separately
            # whether it belongs to a crowd, which is what keeps it off the
            # display grid.
            _write_sidecar(stem, index, label, clip_or_reason,
                           _agent_transform(geometry, index), crowd=count > 1)
            written.append(path)
        else:
            failed.append("agent {} — {}".format(label, clip_or_reason))
            _log("FAILED agent {}: {}".format(label, clip_or_reason))

    # One agent failing must not read the same as all of them succeeding.
    lines = ["{} of {} agent(s) exported to:".format(len(written), len(indices)),
             OUTPUT_DIR, ""]
    lines += [os.path.basename(p) for p in written]
    if failed:
        lines += ["", "Did NOT export:"] + failed
    if len(written) > 1:
        lines += ["",
                  "These are crowd agents: each FBX carries its own world "
                  "position, and a .crowd.json beside it tells the platform to "
                  "place the actor at the origin instead of on the display "
                  "grid — so the crowd re-forms in Unreal as it stands here."]
    handed = _hand_over(written)
    if handed:
        lines += ["", handed]
    in_library = "/outputs/" in (OUTPUT_DIR.replace("\\", "/") + "/")
    if in_library:
        lines += ["",
                  "They are also in the platform's Library, so you can queue "
                  "them there and send them by hand at any time."]
    elif not handed.startswith("Sent"):
        # Files written outside the Library that nothing collected would sit on
        # disk invisible to everything — exactly the kind of quiet nothing this
        # project does not ship.
        lines += ["",
                  "These went outside the platform's Library, so they will NOT "
                  "appear in the gallery, and nothing has sent them anywhere "
                  "either. Start the platform once so it writes its pointer "
                  "file, or point OUTPUT_DIR at "
                  "pro\\workspace\\outputs\\Houdini_Exports to queue them by hand."]

    trouble = bool(failed) or handed.startswith(("Not sent", "Could not",
                                                 "The platform"))
    _tell("\n".join(lines), error=bool(failed and not written), copyable=trouble)
    return written


def _find_platform():
    """The command that runs the platform on *this* machine.

    `SEND_COMMAND` wins if it is set. Otherwise the platform's own pointer
    file: it writes `workspace/exports/platform.json` at every start, recording
    how it was started — which is the only place that answer actually exists.
    A source install is an interpreter inside a venv plus app.py, a released
    build is one executable, and both differ per computer.

    Returns (command, why-not). Exactly one of the two is filled in.
    """
    if SEND_COMMAND:
        return list(SEND_COMMAND), ""

    folder = os.path.abspath(OUTPUT_DIR)
    seen = []
    for _ in range(8):                       # walk up to the workspace
        candidate = os.path.join(folder, "exports", "platform.json")
        seen.append(candidate)
        if os.path.isfile(candidate):
            try:
                with open(candidate) as handle:
                    command = json.load(handle).get("command") or []
            except Exception as exc:
                return [], "{} could not be read ({})".format(candidate, exc)
            if not command:
                return [], "{} names no command".format(candidate)
            if not os.path.exists(command[0]):
                return [], ("{} points at {}, which is not there any more — "
                            "start the platform once to refresh it"
                            .format(candidate, command[0]))
            return [str(part) for part in command], ""
        parent = os.path.dirname(folder)
        if parent == folder:
            break
        folder = parent
    return [], ("no platform.json found above {} (looked in {}). Start the "
                "platform once — it writes the file at every start — or set "
                "SEND_COMMAND at the top of this script."
                .format(OUTPUT_DIR, ", ".join(seen[:3])))


def _hand_over(paths):
    """Ask the platform to send these to Unreal. Returns a sentence to show.

    Success is the process's exit code and nothing else. Houdini also exports
    its own `PYTHONHOME`/`PYTHONPATH` into every child it starts, which makes
    another Python refuse to start with an import error that reads like a
    broken installation — so they are removed for the call, exactly as the
    previous version did. That part was right.
    """
    if not paths:
        return ""
    import subprocess

    base, why_not = _find_platform()
    if not base:
        return "Not sent: {}".format(why_not)

    command = base + ["--send-unreal"] + list(paths)
    _log("handing over: {}".format(" ".join(command)))
    environment = os.environ.copy()
    for variable in ("PYTHONHOME", "PYTHONPATH"):
        environment.pop(variable, None)
    # The platform writes UTF-8. Without saying so, Windows decodes the pipe
    # with the ANSI code page and one arrow in a log line becomes an exception
    # on this side of the call.
    startupinfo = None
    if os.name == "nt":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                env=environment, startupinfo=startupinfo)
    except Exception as exc:
        return "Could not start the platform: {}".format(exc)
    for line in (result.stdout or "").splitlines():
        _log("platform: {}".format(line))
    for line in (result.stderr or "").splitlines():
        _log("platform stderr: {}".format(line))
    if result.returncode == 0:
        return "Sent to Unreal by the platform."
    detail = ((result.stdout or "") + "\n" + (result.stderr or "")).strip()
    return "The platform refused or failed (exit {}).\n{}".format(
        result.returncode, detail or "no output")


def main():
    """Run the export and put any stack trace where it can be copied."""
    try:
        return export_agents()
    except Exception:
        detail = traceback.format_exc()
        _log(detail)
        try:
            if hou.isUIAvailable():
                hou.ui.readMultiInput(
                    "The export stopped. This text is copyable (Ctrl+A, Ctrl+C):",
                    ["Log:"], initial_contents=[detail], buttons=("OK",),
                    severity=hou.severityType.Error, title="SLOP — export agents")
        except Exception:
            pass
        return []


# Paste-into-the-editor and shelf-tool use both want this to run on load. Set
# it to False if you ever `import` this module and call `export_agents()`
# yourself — an import that exports is a surprise, and this is the switch.
RUN_ON_LOAD = True

if RUN_ON_LOAD:
    main()
