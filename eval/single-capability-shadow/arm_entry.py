"""Run a frozen evaluation driver with installed product imports isolated."""
import argparse
import os
from pathlib import Path
import runpy
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('provide an arm script or -c program')
    # Only maintained evaluation helpers are importable here. Product imports
    # still resolve from the interpreter's installed distribution under -I.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    os.environ['SHADOW_ARM_CONFIG'] = str(args.config.resolve(strict=True))
    if command[0] == '-c':
        if len(command) < 2:
            parser.error('-c requires a program')
        sys.argv = ['-c', *command[2:]]
        exec(compile(command[1], '<frozen-arm>', 'exec'), {'__name__': '__main__'})
    else:
        sys.argv = command
        runpy.run_path(command[0], run_name='__main__')


if __name__ == '__main__':
    main()
