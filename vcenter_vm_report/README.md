# vcenter-vm-report

Audit script for VMware vCenter that produces a CSV and an interactive HTML
report of every VM in every cluster, with focus on **catching configuration
mistakes** that are easy to miss in the UI — especially on **vSAN stretched
clusters**, where DRS site affinity and storage policy site locality must
agree or you pay for it in cross-site I/O.

For each VM the script collects:

- **DRS rules** the VM is subject to (VM-VM affinity / anti-affinity, VM-Host
  must / should run on / not run on hosts), with the actual VM and host group
  members
- **Storage policies** for the VM home and every disk, with a full breakdown
  of policy capabilities (FTT, stripe width, RAID type, **site locality**, …)
  and a list of compatible datastores per policy (mirroring the vCenter
  *Storage Compatibility* tab)
- **Tags** (`Category:Tag`) attached to the VM
- **Detected issues**: site mismatches, mixed-policy disks, non-compliance,
  disabled rules, etc.

The HTML report is a single self-contained file — no external assets, opens
straight in any browser. Filterable, sortable, deep-linked.

---

## What it detects

| Severity | Detection |
|----------|-----------|
| **critical** | Stretched-cluster site mismatch — DRS rule with `must run on` pins compute to fault domain X, but storage policy keeps data on fault domain Y. Every read/write crosses the inter-site link. |
| **critical** | VM is currently running on a host **not** in the host group required by a `must run on` rule. |
| **warning**  | Same site-mismatch as above, but with a `should run on` (preferred) rule rather than `must` (required). |
| **warning**  | VM is currently running on a host whose fault domain doesn't match the storage policy locality (runtime mismatch — even without a DRS rule). |
| **warning**  | **Mixed storage policies across disks** of the same VM (e.g. `home` and `Hard_disk_1` use policy A, but `Hard_disk_2` uses policy B). Typical mistake when an admin adds a disk and forgets to set the policy. |
| **warning**  | DRS rule says VM is **not in compliance** with the rule. |
| **warning**  | DRS rule references a host group that is empty or unknown. |
| **warning**  | Storage policy lookup failed for a disk. |
| **info**     | DRS rule is **disabled** — silently a no-op, the protection it should provide is not in effect. |

### How site-mismatch detection actually works

The naive approach (substring match on the word "preferred") would false-fire
constantly because real fault domains have names like
`x00-w01-cl01_primary-az-faultdomain`, not literally `Preferred`. The script
instead:

1. Reads each host's fault domain from `cluster.configurationEx.vsanHostConfig`
2. Asks vSAN management API for the **authoritative preferred fault domain**
   via `VsanVcStretchedClusterSystem.VSANVcGetPreferredFaultDomain`
3. Resolves the storage policy's locality value (e.g. `Preferred Fault Domain`,
   `Non-preferred Fault Domain`, `None`, or a literal FD name) into the actual
   FD name(s) it pins data to
4. Compares against the FDs that DRS rules pin compute to
5. If the resolution fails (e.g. user lacks vSAN read privileges), detection
   gracefully falls back rather than producing false positives

---

## Requirements

- Python 3.9+
- `pyvmomi` — VMware vSphere management SDK
- `vmware-vcenter` — VMware vSphere Automation SDK *(only for tag collection;
  the script gracefully degrades without it)*
- `colorama` — coloured terminal output *(optional; falls back to plain text
  if not installed)*

### vCenter privileges

| Used for | Privilege | Apply at |
|----------|-----------|----------|
| Inventory / DRS rules / hosts | **Read-only** role | datacenter or vCenter root, propagated |
| Storage policies | **VM storage policies → View VM storage policies** *(in vSphere 8.x; previously called `Profile-driven storage view`)* | **vCenter root** (SPBM is vCenter-wide) |
| Tags | The default Read-only role usually grants tag-read; if not, add `Tagging → Read tag` | vCenter root |
| Stretched-cluster preferred FD | vSAN read access on the cluster *(typically the Read-only role suffices; if not, the section just shows a warning instead of false-positives)* | cluster |

The simplest setup is one custom role at the vCenter root that combines
Read-only with `View VM storage policies`, propagated to children.

---

## Installation

### Option 1 — Standard PyPI install

```bash
pip install pyvmomi vmware-vcenter colorama
```

`vmware-vcenter` brings in `vmware-vapi-runtime` and the tagging client
bindings as transitive dependencies. `colorama` is optional — without it the
script just prints plain text instead of coloured output.

### Option 2 — Air-gapped environment with a private PyPI proxy (Nexus, Artifactory)

If you already have a PyPI proxy configured (typical Nexus setup):

```bash
python3 -m pip config --global set global.index-url https://your.proxy/repository/pypi.org/simple
python3 -m pip config --global set global.trusted-host your.proxy
pip install pyvmomi vmware-vcenter colorama
```

All three packages are published on pypi.org so a regular proxy works.

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

Tags are optional. If you only have `pyvmomi` available, the script still runs
and reports DRS rules, storage policies and issues — it just notes:

```
[!] vsphere-automation-sdk not installed, tags will be skipped.
```

---

## Usage

```bash
python vcenter_vm_report.py -s vcenter.example.com -u 'admin@vsphere.local'
```

Arguments:

| Flag | Description |
|------|-------------|
| `-s`, `--server`   | vCenter hostname or IP **(required)** |
| `-u`, `--user`     | Username **(required)** |
| `-p`, `--password` | Password (prompted if omitted) |
| `-o`, `--output`   | Output filename base, `.csv` and `.html` are appended. Default: `vm_report_<vcenter>_<timestamp>` (e.g. `vm_report_vc-mgmt-a_2026-04-29_15-30-45.csv`) so each run produces unique files |
| `--cluster`        | Limit the report to a single cluster |

Self-signed certificates are accepted by default — the script always uses
`ssl.CERT_NONE`. This matches the typical internal vCenter / VCF lab setup;
if you need strict TLS verification, modify `connect_vcenter()` directly.

Password input on Windows uses `ReadConsoleW` so passwords with Czech /
Slovak / Polish accented characters typed via Alt-codes work correctly.

The script writes two files:

- `<base>.csv` — flat tabular view, one row per VM, suitable for grep / Excel
- `<base>.html` — interactive report (see below)

While running, it prints per-VM progress and flags problems immediately:

```
[*] Connecting to vc-mgmt-a.site-a.vcf.lab ...
[OK] Connected - VMware vCenter Server 8.0.3 (build 24091160)
[*] Connecting to SPBM ...
[OK] SPBM ready
[*] Connecting to tagging API ...
[OK] Tagging API ready
[*] Total: 2 cluster(s), 480 VM(s)

[*] Cluster: x00-w01-cl01
    Fault domains: x00-w01-cl01_primary-az-faultdomain (preferred),
                   x00-w01-cl01_secondary-az-faultdomain
    [1/480] vm-prod-01 ... rules=1, policies=2, tags=1, issues=0
    [2/480] vm-prod-db01 ... rules=0, policies=2, tags=2, issues=1 [!] 1w
    ...
[*] Resolving storage compatibility for 12 policies ...
    [1/12] vSAN ESA Default Policy - RAID5 ... 1 compatible
    [2/12] W01-vSAN-Site-X00 ... 1 compatible
    ...
[*] Writing CSV: vm_report_vc-mgmt-a_2026-04-29_15-30-45.csv
[*] Writing HTML: vm_report_vc-mgmt-a_2026-04-29_15-30-45.html
[+] Done. Critical: 0, Warnings: 2.
```

Markers next to the VM line: `Nc/Nw` = N critical and N warning issues found.

---

## HTML report layout

The report has a sticky header with summary tiles, a navigation bar, a filter
bar, and four navigable sections.

### Summary tiles (interactive)

```
0 critical   |   2 warnings   |   0 info   |   478 clean
```

The four tiles in the header are **clickable filters**. Click *2 warnings* and
the VM table immediately filters to just those two VMs; click again to clear.
Click *478 clean* to see only the VMs without any issues. Each tile lights up
with a colored outline when active, so you always know what you're looking at.

### 1. VM tables (per cluster)

Compact rows with:
- VM name, host, power state
- **Issues** — colour-coded blocks (red / yellow / blue) for each detected
  problem
- **DRS rules** — short type label + rule name as a clickable link (jumps to
  the rule detail card in section 3)
- **Storage policy** — disk label + policy name as a clickable link, with a
  `LOCALITY` flag if the policy has site-affinity rules
- Tags

The filter bar above the table has free-text search, a cluster picker, an
issue-severity dropdown (all / any / critical only / warning only / critical +
warning / info only / no issues), and a DRS-rule-presence filter. Click any
column header to sort.

### 2. Storage Policies catalog

Every unique storage policy referenced by any VM, alphabetical, each shown as
a detail card with:
- Full capability list (e.g. `VSAN.hostFailuresToTolerate = 1`,
  `VSAN.locality = Site-A`, …) with locality rows highlighted in yellow
- **Used by** — list of VMs and which disks reference this policy
- **Compatible datastores** — same data as the vCenter UI's Storage
  Compatibility tab: name, type (vSAN / VMFS / NFS / Datastore Cluster),
  capacity, free space, and a usage bar that turns yellow under 25 % free or
  red under 10 % free

### 3. DRS Rules catalog

Every unique DRS rule, grouped per cluster, each as a detail card with:
- Required / Preferred / Compliant / Not Compliant / Disabled badges
- Full member list (for VM-VM rules)
- VM group + Host group with all members expanded (for VM-Host rules)
- **Affected VMs** — which VMs in the report fall under this rule

### 4. Fault Domains

A topology map of every cluster showing how hosts map to fault domains.
For stretched clusters the **preferred** FD is highlighted with a green
outline and a *Preferred* badge. Each FD card shows:
- Aggregate stats: host count, active hosts, total VMs, total cores, total RAM
- Per-host table: name, connection state, VM count, cores, RAM
- Maintenance-mode hosts are flagged in yellow, disconnected ones in red

If the cluster is stretched but the preferred FD couldn't be determined
(typically a privilege issue), an inline yellow warning appears on that
cluster instead — site-mismatch detection then operates in a more conservative
mode rather than producing false positives.

### Anchors

All sections deep-link, so you can share or bookmark a specific item:
- `report.html#vms` / `#policies` / `#rules` / `#faultdomains`
- `report.html#policy-vsan-default-storage-policy`
- `report.html#rule-cluster-mgmt-01a-nsx-edges-antiaffinity`

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

- **PBM is queried per VM and per disk.** Profiles are cached, so large
  environments are still tractable, but the initial pass on a cluster with
  thousands of VMs takes minutes, not seconds.
- **Storage compatibility resolution** uses one extra `PbmQueryMatchingHub`
  call per unique policy after the per-VM pass. On environments with many
  policies this adds a small amount of time at the end of the run.
- **Site-mismatch detection requires preferred-FD resolution.** If the user
  lacks the vSAN read privileges needed for `VSANVcGetPreferredFaultDomain`,
  the script falls back to literal FD-name matching only — meaning a policy
  saying `Preferred Fault Domain` won't be evaluated against your topology.
  The fault-domain section in the HTML shows a yellow warning when this
  happens, so you can spot affected clusters at a glance.
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
the latest revision; the current code is 3.11-compatible.

**`SPBM unavailable: <error>`** *or* storage policy column is empty
The user is missing the **VM storage policies → View VM storage policies**
privilege. In vSphere 8 this privilege has to be assigned at the **vCenter
root** object (SPBM is vCenter-wide). The Read-only role alone does **not**
include it. Create a custom role that combines Read-only with that one
privilege and propagate from root.

**`Tagging API failed`** *or* `vsphere-automation-sdk not installed`
`vmware-vcenter` package is missing or the user can't read the CIS tagging
API. Tags will be empty; everything else works. Install it with
`pip install vmware-vcenter`.

**`Could not determine preferred fault domain`** *(per-cluster warning)*
The user lacks read access to the vSAN management API for that cluster. Site
mismatch detection on that cluster falls back to literal FD-name matches.
Either give the user vSAN read privileges or accept that the report will only
flag the obvious cases.

**`Cluster '<name>' not found`**
The `--cluster` argument is matched case-sensitively against
`ClusterComputeResource.name`.

**Password with accented characters not accepted on Windows**
The script uses `ReadConsoleW` instead of standard `getpass`, so Alt-code
sequences (Alt+0xxx) on Czech / Slovak / Polish keyboards work correctly.
If you still see issues, check that you're running it in a real Windows
console (cmd.exe / PowerShell), not in a terminal that intercepts input.

---

## Example: site mismatch finding

A real anti-pattern this script catches:

```
[CRITICAL] Site mismatch: DRS rule 'AppB-Site-B-MustRun' says VM must
run on hosts in ['x00-w01-cl01_secondary-az-faultdomain'], effectively
pinning compute to ['x00-w01-cl01_secondary-az-faultdomain']; but
storage policy on home requires data on Preferred Fault Domain
(= ['x00-w01-cl01_primary-az-faultdomain']). Cross-site I/O for every R/W.
```

In the UI this looks fine — the rule is enabled, compliant, the policy is
applied — but the combined effect is that every storage operation traverses
the inter-site link. The script flags this in seconds, and the HTML report
shows you exactly which policy and which rule are in conflict so you can
fix it.

---

## License

MIT. Use it, fork it, ship it.
