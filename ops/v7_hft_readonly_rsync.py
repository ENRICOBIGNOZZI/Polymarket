#!/usr/bin/env python3
"""Forced SSH command: sender-only access to the fixed HFT evidence directory."""
import os
from pathlib import Path
import re
import shlex
import sys


def sender_args(command, root):
    args=shlex.split(command)
    if len(args)!=6 or args[:3]!=['rsync','--server','--sender'] or args[4]!='.':
        raise ValueError('ONLY_FIXED_RESEARCH_RSYNC_SENDER_ALLOWED')
    if not re.fullmatch(r'-[A-Za-z.]+',args[3]) or any(c in args[3].split('e.')[0] for c in 'LKks'):
        raise ValueError('UNSUPPORTED_RSYNC_OPTIONS')
    root=Path(root).resolve()
    if root.name!='hft_permanent' or root.parent.name!='research' or args[5].rstrip('/')!=str(root):
        raise ValueError('ONLY_FIXED_RESEARCH_PATH_ALLOWED')
    return ['/usr/bin/rsync','--server','--sender',args[3],'.',str(root)+'/']


if __name__=='__main__':
    try: args=sender_args(os.environ.get('SSH_ORIGINAL_COMMAND',''),sys.argv[1])
    except (ValueError,IndexError): sys.exit(126)
    os.execv(args[0],args)
