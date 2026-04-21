#!/usr/bin/env python3
"""
generate_hosts_config.py

Generate a VCF-style hosts configuration JSON file from a list of FQDNs.

DESCRIPTION:
    This script reads a list of fully qualified host domain names (FQDNs) from
    a file named 'hosts.json' located in the same directory as this script,
    then interactively prompts the user for the remaining host parameters
    (username, password, network pool name, storage type, and vVol storage
    protocol type). The same set of parameters is applied to every host
    loaded from 'hosts.json'.

    The password is entered in hidden mode (characters are NOT echoed to the
    terminal) using the standard 'getpass' module, which is fully supported
    on Windows 10 command line (cmd.exe and PowerShell).

    For the enumerated parameter 'storageType' the user is presented with a
    numbered menu and selects the value by typing the corresponding number.
    The additional 'vvolStorageProtocolType' menu is shown ONLY when the
    selected storage type is 'VVOL'. For any other storage type the prompt
    is skipped and the 'vvolStorageProtocolType' field is omitted from the
    resulting JSON file.

    VCF accepts a maximum of 50 hosts per commissioning file. When the input
    contains more than 50 FQDNs the output is split into multiple files with
    at most 50 hosts each. In that case a zero-padded '_partNN' suffix is
    inserted before the file extension of the configured output path, for
    example 'hosts_config.json' becomes 'hosts_config_part01.json',
    'hosts_config_part02.json', and so on. When the input has 50 FQDNs or
    fewer a single output file is written using the exact configured name.

INPUT FILE FORMAT (hosts.json):
    A JSON array of FQDN strings, for example:

    [
        "esx-01a.site-a.vcf.lab",
        "esx-02a.site-a.vcf.lab",
        "esx-03a.site-a.vcf.lab",
        "esx-04a.site-a.vcf.lab",
        "esx-05a.site-a.vcf.lab"
    ]

OUTPUT FILE FORMAT (hosts_config.json by default):
    When storageType is NOT 'VVOL' the 'vvolStorageProtocolType' field is
    omitted:

    {
        "hosts": [
            {
                "fqdn": "esx-01a.site-a.vcf.lab",
                "username": "root",
                "storageType": "VSAN",
                "password": "********",
                "networkPoolName": "sfo-m01-np01"
            },
            ...
        ]
    }

    When storageType IS 'VVOL' the 'vvolStorageProtocolType' field is
    included:

    {
        "hosts": [
            {
                "fqdn": "esx-01a.site-a.vcf.lab",
                "username": "root",
                "storageType": "VVOL",
                "password": "********",
                "networkPoolName": "sfo-m01-np01",
                "vvolStorageProtocolType": "VMFS_FC"
            },
            ...
        ]
    }

USAGE:
    python generate_hosts_config.py
    python generate_hosts_config.py --input hosts.json --output hosts_config.json
    python generate_hosts_config.py --help

EXAMPLES:
    # Run with defaults (reads ./hosts.json, writes ./hosts_config.json):
    python generate_hosts_config.py

    # Specify custom input and output file paths:
    python generate_hosts_config.py -i my_hosts.json -o my_output.json

REQUIREMENTS:
    - Python 3.6 or newer
    - Windows 10 / Windows 11 / Linux / macOS
    - No third-party packages required (uses only the standard library)

EXIT CODES:
    0  Success
    1  Input file not found or invalid JSON
    2  Invalid user input or user aborted (Ctrl+C)
"""

import argparse
import getpass
import json
import os
import sys
import textwrap

# Allowed values for the enumerated fields.
STORAGE_TYPES = [
    "VSAN",
    "VSAN_REMOTE",
    "VSAN_ESA",
    "VSAN_MAX",
    "NFS",
    "VMFS_FC",
    "VVOL",
]

VVOL_PROTOCOL_TYPES = [
    "VMFS_FC",
    "ISCSI",
    "NFS",
]

# VCF imposes a 50-host limit per commissioning operation. When the input
# contains more than this, the output is split into multiple files, each
# containing at most MAX_HOSTS_PER_FILE host entries.
MAX_HOSTS_PER_FILE = 50


def parse_arguments():
    """Parse command line arguments and return the argparse Namespace."""
    parser = argparse.ArgumentParser(
        prog="generate_hosts_config.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent(__doc__),
    )
    parser.add_argument(
        "-i",
        "--input",
        default="hosts.json",
        help=(
            "Path to the input JSON file containing an array of FQDN strings. "
            "Default: 'hosts.json' in the same directory as the script."
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        default="hosts_config.json",
        help=(
            "Path to the output JSON file that will be created. "
            "Default: 'hosts_config.json' in the current working directory."
        ),
    )
    return parser.parse_args()


def load_fqdns(input_path):
    """Load and validate the FQDN list from the input JSON file."""
    if not os.path.isfile(input_path):
        print(f"ERROR: Input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    try:
        with open(input_path, "r", encoding="utf-8") as fp:
            data = json.load(fp)
    except json.JSONDecodeError as exc:
        print(f"ERROR: Input file is not valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    except OSError as exc:
        print(f"ERROR: Could not read input file: {exc}", file=sys.stderr)
        sys.exit(1)

    if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
        print(
            "ERROR: Input file must contain a JSON array of FQDN strings.",
            file=sys.stderr,
        )
        sys.exit(1)

    if len(data) == 0:
        print("ERROR: Input file contains no FQDNs.", file=sys.stderr)
        sys.exit(1)

    return data


def prompt_non_empty(prompt_text):
    """Prompt the user for a non-empty string value."""
    while True:
        value = input(prompt_text).strip()
        if value:
            return value
        print("  Value cannot be empty. Please try again.")


def prompt_password(prompt_text="Password: "):
    """Prompt the user for a password with hidden input and confirmation."""
    while True:
        try:
            pwd1 = getpass.getpass(prompt_text)
            pwd2 = getpass.getpass("Confirm password: ")
        except (EOFError, KeyboardInterrupt):
            print("\nAborted by user.", file=sys.stderr)
            sys.exit(2)

        if not pwd1:
            print("  Password cannot be empty. Please try again.")
            continue
        if pwd1 != pwd2:
            print("  Passwords do not match. Please try again.")
            continue
        return pwd1


def prompt_choice(title, options):
    """Display a numbered menu and return the value selected by the user."""
    print()
    print(title)
    for idx, value in enumerate(options, start=1):
        print(f"  {idx}) {value}")

    while True:
        raw = input(f"Select an option [1-{len(options)}]: ").strip()
        if raw.isdigit():
            num = int(raw)
            if 1 <= num <= len(options):
                return options[num - 1]
        print(f"  Invalid choice. Please enter a number between 1 and {len(options)}.")


def build_host_entries(fqdns, common_params):
    """Build the list of host dictionaries using the shared parameters.

    The 'vvolStorageProtocolType' field is only included when storageType is
    'VVOL'; for every other storage type it is omitted from the output.
    """
    hosts = []
    for fqdn in fqdns:
        entry = {
            "fqdn": fqdn,
            "username": common_params["username"],
            "storageType": common_params["storageType"],
            "password": common_params["password"],
            "networkPoolName": common_params["networkPoolName"],
        }
        if common_params["storageType"] == "VVOL":
            entry["vvolStorageProtocolType"] = common_params["vvolStorageProtocolType"]
        hosts.append(entry)
    return hosts


def chunk_list(items, chunk_size):
    """Yield successive chunks of the given size from a list."""
    for start in range(0, len(items), chunk_size):
        yield items[start:start + chunk_size]


def build_chunked_output_paths(output_path, num_chunks):
    """Return a list of output paths, one per chunk.

    If there is only a single chunk the original path is returned unchanged.
    Otherwise a zero-padded '_partNN' suffix is inserted before the file
    extension, for example 'hosts_config.json' -> 'hosts_config_part01.json'.
    """
    if num_chunks <= 1:
        return [output_path]

    root, ext = os.path.splitext(output_path)
    width = max(2, len(str(num_chunks)))
    return [
        f"{root}_part{str(idx).zfill(width)}{ext}"
        for idx in range(1, num_chunks + 1)
    ]


def write_output(output_path, payload):
    """Write the final JSON payload to disk."""
    try:
        with open(output_path, "w", encoding="utf-8") as fp:
            json.dump(payload, fp, indent=4)
            fp.write("\n")
    except OSError as exc:
        print(f"ERROR: Could not write output file: {exc}", file=sys.stderr)
        sys.exit(1)


def main():
    args = parse_arguments()

    # Resolve the input path relative to the script's directory when the user
    # did not override the default, so running from another CWD still works.
    if args.input == "hosts.json":
        script_dir = os.path.dirname(os.path.abspath(__file__))
        input_path = os.path.join(script_dir, "hosts.json")
    else:
        input_path = args.input

    fqdns = load_fqdns(input_path)

    print(f"Loaded {len(fqdns)} host(s) from: {input_path}")
    print("The values you enter below will be applied to ALL hosts.")
    print("-" * 60)

    try:
        username = prompt_non_empty("Username: ")
        password = prompt_password("Password (input hidden): ")
        network_pool_name = prompt_non_empty("Network Pool Name: ")

        storage_type = prompt_choice(
            "Select storage type:",
            STORAGE_TYPES,
        )

        # The vVol protocol type is only relevant when storageType is VVOL.
        # For any other storage type we skip the prompt entirely and the
        # 'vvolStorageProtocolType' field is omitted from the output file.
        if storage_type == "VVOL":
            vvol_protocol = prompt_choice(
                "Select vVol storage protocol type:",
                VVOL_PROTOCOL_TYPES,
            )
        else:
            vvol_protocol = None
    except KeyboardInterrupt:
        print("\nAborted by user.", file=sys.stderr)
        sys.exit(2)

    common_params = {
        "username": username,
        "password": password,
        "networkPoolName": network_pool_name,
        "storageType": storage_type,
        "vvolStorageProtocolType": vvol_protocol,
    }

    all_hosts = build_host_entries(fqdns, common_params)

    # VCF accepts at most MAX_HOSTS_PER_FILE hosts per commissioning file,
    # so we split the output into multiple files when the list is larger.
    chunks = list(chunk_list(all_hosts, MAX_HOSTS_PER_FILE))
    output_paths = build_chunked_output_paths(args.output, len(chunks))

    for chunk, path in zip(chunks, output_paths):
        write_output(path, {"hosts": chunk})

    print("-" * 60)
    if len(chunks) == 1:
        print(
            f"SUCCESS: Wrote {len(all_hosts)} host(s) to: "
            f"{os.path.abspath(output_paths[0])}"
        )
    else:
        print(
            f"SUCCESS: Wrote {len(all_hosts)} host(s) split across "
            f"{len(chunks)} files (max {MAX_HOSTS_PER_FILE} hosts each):"
        )
        for chunk, path in zip(chunks, output_paths):
            print(f"  - {os.path.abspath(path)}  ({len(chunk)} hosts)")


if __name__ == "__main__":
    main()
