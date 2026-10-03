# Licensing and private forks

On 3 October 2026 the maintainer delegated the project-license selection to the
development agent. **Apache License 2.0 (`Apache-2.0`)** was selected for AOS's
original source and documentation. The authoritative terms are in [LICENSE](../LICENSE).
This replaces the earlier pending-license status; historical observations remain
dated records, not current restrictions. It does not certify release acceptance.

The license text is the unmodified English text from the
[Apache Software Foundation](https://www.apache.org/licenses/LICENSE-2.0.txt),
retrieved on 3 October 2026, SHA-256
`cfc7749b96f63bd31c3c42b5c471bf756814053e847c10f3eb003417bc523d30`.
Python, UI and native-shell metadata use the same SPDX identifier.

## Why this choice

The intended public-source/private-enterprise-fork workflow fits a permissive
license. Apache-2.0 allows use, modification and distribution under its terms,
and includes an explicit contributor patent grant with defined conditions.
It does not require publishing a company's private modifications merely because
the company uses the software internally. Distribution has separate obligations.
The license text, not this summary, controls; have your organization review its
actual distribution, dependencies, rights and compliance requirements.

## Boundaries that remain

- This selection covers AOS's original work. It does not relicense dependencies,
  copied third-party material, model/backend code, weights, adapters or datasets.
  Their own licenses and applicable notices still apply.
- Source archives exclude installed dependency trees, model weights, real
  trajectories, screenshots, credentials and private company data. Licensing the
  source does not authorize sharing those artifacts or provide data/training rights.
- Redistribution must satisfy the license's requirements, including supplying
  the license, retaining applicable notices and identifying modifications. If an
  upstream work supplies relevant NOTICE content, preserve it as required. No
  invented third-party attribution or blanket compatibility claim is supplied here.
- The license provides no general trademark permission or warranty. An open-source
  license is not production readiness, a security audit, runtime authorization,
  GPU acceptance or proof of ownership of every external contribution.
- Contributors must have the rights to submit their changes. Maintainers must
  review provenance and third-party obligations when importing new material.

Keep application-specific configuration, intranet credentials and authorized
learning data private in a company fork. See [source handoff](SOURCE_HANDOFF.md)
and [Claude continuation](CLAUDE_HANDOFF.md). Changes to the project license
require a new explicit maintainer decision, not an automated deployment action.
