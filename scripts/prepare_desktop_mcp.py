import argparse
from pathlib import Path

from aos.desktop_mcp_bundle import prepare_bundle


def main():
    parser = argparse.ArgumentParser(description='Pin existing local MCP packages; no downloads or image changes')
    parser.add_argument('--node-modules', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True, help='New private local artifact directory')
    arguments = parser.parse_args()
    print(prepare_bundle(arguments.node_modules, arguments.output))


if __name__ == '__main__':
    main()
