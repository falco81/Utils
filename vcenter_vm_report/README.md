# vcenter-vm-report

Audit script for VMware vCenter that produces a CSV and an interactive HTML report
of every VM in every cluster, with focus on **catching configuration mistakes**
that are easy to miss in the UI — especially on **vSAN stretched clusters**, where
DRS site affinity and storage policy site locality must agree or you pay for it
in cross-site I/O.

For each VM the script collects:

- **DRS rules** the VM is subject to (VM-VM affinity / anti-affinity, VM-Host
  must/should run / not run on hosts), with the actual VM and host group members
- **Storage policies** for the VM home and every disk, including a full breakdown
  of policy capabilities (FTT, stripe width, RAID type, **site locality**, …)
- **Tags** (`Category:Tag`) attached to the VM
- **Detected issues**: site mismatches, non-compliance, disabled rules, etc.

The HTML report is a single self-contained file — no external assets, opens
straight in any browser. Filterable, sortable, deep-linked.

---

## What it detects

| Severity | Detection |
|----------|-----------|
| **critical** | Stretched cluster site mismatch — DRS rule pins compute to fault domain X, but storage policy keeps data on fault domain Y. Every read/write crosses the inter-site link. |
| **critical** | VM is currently running on a host **not** in the host group required by a "must run on" rule. |
| **warning** | Same site-mismatch case as above, but with a "should run on" (preferred) rule rather than "must" (required). |
| **warning** | VM is currently running on a host whose fault domain doesn't match the storage policy locality (runtime mismatch — even without a rule). |
| **warning** | DRS rule says VM is **not in compliance** with the rule. |
| **warning** | DRS rule references a host group that is empty or unknown. |
| **warning** | Storage policy lookup failed for a disk. |
| **info** | DRS rule is **disabled** — silently no-op, the protection it should provide is not in effect. |

The detection logic compares the policy's `locality` capability (or any
capability whose name contains *locality*, *siteAffinity*, *dataLocality*) against
the fault domains of the hosts that DRS pins the VM to.

---

## Requirements

- Python 3.9+
- `pyvmomi` — VMware vSphere management SDK
- `vmware-vcenter` — VMware vSphere Automation SDK (only for tag collection;
  the script gracefully degrades without it)
- `colorama` — coloured terminal output (optional; falls back to plain text
  if not installed)

The vCenter user needs read access to clusters, hosts, VMs, storage profiles
(`StorageProfile.View`) and tags.

---

## Installation

### Option 1 — Standard PyPI install

```bash
pip install pyvmomi vmware-vcenter colorama
```

`vmware-vcenter` brings in `vmware-vapi-runtime` and the tagging client bindings
as transitive dependencies. `colorama` is optional — script runs fine without
it, just without colored terminal output.

### Option 2 — Air-gapped environment with a private PyPI proxy (Nexus, Artifactory)

If you already have a PyPI proxy configured (typical Nexus setup):

```bash
python3 -m pip config --global set global.index-url https://your.proxy/repository/pypi.org/simple
python3 -m pip config --global set global.trusted-host your.proxy
pip install pyvmomi vmware-vcenter
```

Both packages are published on pypi.org so a regular proxy works.

### Option 3 — Fully offline (no proxy)

On a machine with internet access:

```bash
pip download pyvmomi vmware-vcenter colorama -d ./wheels
```

Transfer `./wheels` to the target host and:

```bash
pip install --no-index --find-links=./wheels pyvmomi vmware-vcenter colorama
```

### Option 4 — Skip tagging entirely

Tags are optional. If you only have `pyvmomi` available, the script still
runs and reports DRS rules, storage policies and issues — it just notes:

```
[!] vsphere-automation-sdk not installed, tags will be skipped.
```

---

## Usage

```bash
python vcenter_vm_report.py -s vcenter.example.com -u 'admin@vsphere.local' -o report
```

Arguments:

| Flag | Description |
|------|-------------|
| `-s`, `--server` | vCenter hostname or IP (required) |
| `-u`, `--user` | Username (required) |
| `-p`, `--password` | Password (prompted if omitted) |
| `-o`, `--output` | Output filename base, `.csv` and `.html` are appended. Default: `vm_report_<vcenter>_<timestamp>` (e.g. `vm_report_vc-mgmt-a_2026-04-29_15-30-45.csv`) so each run produces unique files |
| `--cluster` | Limit the report to a single cluster |

Self-signed certificates are accepted by default — the script always uses
`ssl.CERT_NONE`. This matches the typical internal vCenter / VCF lab setup;
if you need strict TLS verification, modify `connect_vcenter()` directly.

Password input on Windows uses `ReadConsoleW` so passwords with Czech /
Slovak / Polish accented characters typed via Alt-codes work correctly.

The script writes two files:

- `report.csv` — flat tabular view, one row per VM, suitable for grep / Excel
- `report.html` — interactive report (see below)

While running, it prints per-VM progress and flags problems immediately:

```
[*] Cluster: cluster-mgmt-01a
    Fault domains: Site-A, Site-B
    [1/35] vm-test03 ... rules=0, policies=2, tags=0, issues=0
    [2/35] app-b-01 ... rules=1, policies=2, tags=2, issues=4 [!] 2c/2w
    ...
```

`2c/2w` = 2 critical, 2 warnings.

---

## HTML report layout

The report is split into three navigable sections:

### 1. VM tables (per cluster)

Compact rows with:
- VM name, host, power state
- **Issues** column — colour-coded blocks (red / yellow / blue) for each detected
  problem
- **DRS rules** — short type label + rule name as a clickable link (jumps to
  the rule detail card below)
- **Storage policy** — disk label + policy name as a clickable link, with a
  `LOCALITY` flag if the policy has site-affinity rules
- Tags

Filter bar above the table: free-text search, cluster picker, severity filter
(all / any issue / critical only / critical + warning / no issues), DRS-rule
presence filter. Click any column header to sort.

### 2. Storage Policies catalog

Every unique storage policy referenced by any VM, alphabetical, each shown as a
detail card with:
- Full capability list (e.g. `VSAN.hostFailuresToTolerate = 1`,
  `VSAN.locality = Site-A`, …) with locality rows highlighted
- "Used by" — list of VMs and which disks reference this policy

### 3. DRS Rules catalog

Every unique DRS rule, grouped per cluster, each as a detail card with:
- Required / Preferred / Compliant / Not Compliant / Disabled badges
- Full member list (for VM-VM rules)
- VM group + Host group with all members expanded (for VM-Host rules)
- "Affected VMs" — which VMs in the report fall under this rule

Anchors are stable, so you can link to a specific rule or policy directly:
`report.html#policy-vsan-default-storage-policy`,
`report.html#rule-cluster-mgmt-01a-nsx-edges-antiaffinity`.

---

## CSV columns

| Column | Description |
|--------|-------------|
| `cluster` | Cluster name |
| `host` | Host the VM is currently running on |
| `vm` | VM name |
| `power_state` | `poweredOn` / `poweredOff` / `suspended` |
| `drs_rules` | Human-readable rule descriptions, `;;`-separated |
| `storage_policies` | `home=PolicyName[capability=value; …] ;; Hard_disk_1=…` |
| `tags` | `Category:Tag ;; …` |
| `issues` | `[CRITICAL] message ;; [WARNING] message ;; …` |

---

## Limitations

- **Locality matching is heuristic.** The script matches a storage policy's
  `locality` value against fault domain names by exact match, substring match,
  and the `preferred` / `secondary` keyword convention. If your fault domains
  are named in a way that breaks this heuristic, the report may flag false
  positives — review the message and add an exception if needed.
- **Fault domain detection** reads `cluster.configurationEx.vsanHostConfig`. If
  your stretched cluster doesn't expose this through the standard API, the FD
  map will be empty and site-mismatch detection is skipped (the rest of the
  report still works).
- **PBM is queried per VM and per disk.** Profiles are cached, so large
  environments are still tractable, but the initial pass on a cluster with
  thousands of VMs takes minutes, not seconds.
- The **rule name as written in vCenter is preserved verbatim**, including
  long auto-generated suffixes like
  `VCF-edge_edgecl-mgmt-a_antiAffinity_8986743f13c7d2ec68ba2e2cfd338f2a`.
  These names are how vCenter identifies the rule; the script can't shorten
  them safely. The HTML truncates them in the main table and shows the full
  name in the rule detail card.

---

## Troubleshooting

**`SyntaxError: f-string expression part cannot include a backslash`**
You're on Python 3.11 or older with an outdated copy of the script. Update to
the latest revision.

**`SPBM unavailable: <error>`**
Storage policy lookup needs the PBM endpoint reachable from your client and
the user privilege `Profile-driven storage view`. Without it, the report
still runs but the storage policy column will be empty.

**`Tagging API failed`**
`vmware-vcenter` is missing or the user can't read the CIS tagging API. Tags
will be empty; everything else works.

**`Cluster '<name>' not found`**
The `--cluster` argument is matched case-sensitively against
`ClusterComputeResource.name`.

---

## Example: site mismatch finding

A real anti-pattern this script catches:

```
[CRITICAL] Site mismatch: DRS rule 'AppB-Site-B-MustRun' says VM must
run on hosts in ['Site-B'], effectively pinning compute to ['Site-B'];
but storage policy on home requires data on 'Site-A' (locality).
Cross-site I/O for every R/W.
```

In the UI this looks fine — the rule is enabled, compliant, the policy is
applied — but the combined effect is that every storage operation traverses
the inter-site link. The script flags this in seconds.

---

## License

MIT. Use it, fork it, ship it.
