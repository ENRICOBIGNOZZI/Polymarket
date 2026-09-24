"""Read-only, bounded, rotation-aware append-tape health. Unknown is not zero."""
from __future__ import annotations
import hashlib
import time


def sample_tape(current, *, generation=None, rows_written=None, maximum_files=4096):
    # Native book tapes have a dedicated directory and session-prefixed segments.
    # Other journals share directories: never count a neighbour's writes.
    patterns = ["*.segment-*.jsonl"] if current.name == "current.jsonl" else [
        current.name + ".segment-*.jsonl", current.stem + ".segment-*" + current.suffix]
    paths=[current]
    for pattern in patterns:
        for path in current.parent.glob(pattern):
            paths.append(path)
            if len(paths)>maximum_files:
                return {"state":"SCAN_BOUND_EXCEEDED","files":{},"generation":generation,"rows_written":None}
    unique={str(p):p for p in paths}
    files={};missing=[]
    if len(unique)>maximum_files:
        return {"state":"SCAN_BOUND_EXCEEDED","files":{},"generation":generation,"rows_written":None}
    for path in unique.values():
        try:
            if path.is_symlink():raise OSError("symlink")
            with path.open("rb") as stream:
                import os
                st=os.fstat(stream.fileno());prefix=stream.read(min(64,st.st_size))
            key=str(st.st_dev)+":"+str(st.st_ino)
            files[key]={"path":path.name,"bytes":st.st_size,"mtime_ns":st.st_mtime_ns,
                        "prefix_length":len(prefix),"prefix_hex":prefix.hex(),
                        "prefix_sha256":hashlib.sha256(prefix).hexdigest()}
        except OSError:missing.append(path.name)
    total_bytes=sum(f["bytes"] for f in files.values())
    if type(rows_written) is not int or rows_written<0:rows_written=None
    # A populated tape with a zero writer counter is not evidence of zero rows;
    # treat that counter as unavailable instead of overriding byte-level liveness.
    if rows_written==0 and total_bytes>0:rows_written=None
    return {"state":"OBSERVED" if files else "UNAVAILABLE","files":files,"generation":generation,
            "rows_written":rows_written,"missing":missing,"timestamp_ns":time.time_ns(),
            "current_and_retained_segment_bytes":total_bytes}


def tape_growth(before,after):
    base={"bytes_added_lower_bound":None,"rows_added":None,"exact_rows":False,"live":None,
          "rotated":False,"removed_segments":0,"state":"UNAVAILABLE"}
    if before.get("state")!="OBSERVED" or after.get("state")!="OBSERVED":return base
    a,b=before["files"],after["files"]
    if before.get("generation") and after.get("generation")!=before["generation"]:
        return {**base,"state":"WRITER_GENERATION_CHANGED"}
    removed=set(a)-set(b);added=0;rotated=False
    for key,f in b.items():
        prior=a.get(key)
        if prior is None:added+=f["bytes"];continue
        if f["bytes"]<prior["bytes"] or ("prefix_hex" in prior and "prefix_hex" in f
                and not f["prefix_hex"].startswith(prior["prefix_hex"])) or (prior["prefix_length"]==f["prefix_length"]
                and prior["prefix_sha256"]!=f["prefix_sha256"]):
            return {**base,"state":"TRUNCATION_OR_INODE_REUSE"}
        rotated |= prior["path"]!=f["path"]
        added+=f["bytes"]-prior["bytes"]
    result={**base,"state":"RETENTION_GAP" if removed else "CONTINUOUS",
            "bytes_added_lower_bound":added,"rotated":rotated,"removed_segments":len(removed),
            "live":True if added>0 else None if removed else False}
    x,y=before.get("rows_written"),after.get("rows_written")
    if before.get("generation") and type(x) is int and type(y) is int:
        if y<x:return {**base,"state":"WRITER_COUNTER_REGRESSION"}
        delta=y-x
        if delta>0:
            result.update(rows_added=delta,exact_rows=True,live=True)
        elif added>0:
            # The filesystem proves new append bytes even though the auxiliary
            # row counter did not advance. Preserve byte-level liveness and
            # downgrade the stale counter instead of turning a live tape red.
            result.update(rows_added=None,exact_rows=False,live=True)
        elif removed:
            result.update(rows_added=None,exact_rows=False,live=None)
        else:
            result.update(rows_added=0,exact_rows=True,live=False)
    return result
