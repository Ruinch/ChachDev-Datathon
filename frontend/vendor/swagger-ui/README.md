# Local Swagger UI assets

The following files come unmodified from swagger-ui-dist **5.33.3**, published by the Swagger project:

- `swagger-ui-bundle.js` and `swagger-ui.css` — local browser assets.
- `LICENSE` and `NOTICE` — Swagger UI licensing and attribution.
- `swagger-ui-bundle.js.LICENSE.txt` — bundled third-party license notices, referenced by the JavaScript bundle. Preserve this file together with the bundle; the package license does not replace these notices.

- Package: https://registry.npmjs.org/swagger-ui-dist/-/swagger-ui-dist-5.33.3.tgz
- npm SHA-512 integrity: sha512-7xJKxAjf+/ajlW/V/dMfbiY+NC5BWtbvzzdI/AsX5UzMtOMudmFz/tjuKN0jN7QVe5drKKnx01CdYEJvTFvQlw==
- Package license: Apache-2.0; see LICENSE. Bundled dependencies have their own terms in `swagger-ui-bundle.js.LICENSE.txt`.
- Official usage: https://swagger.io/docs/open-source-tools/swagger-ui/usage/installation/

The API serves these files locally for /docs. External schema validation and persisted authorization are disabled. Upgrade the files together, verify the package integrity, preserve licensing, then check authorization and API schemas in the browser.

Verification on 2026-10-18: the official npm archive matched the pinned SHA-512 integrity. The JavaScript, CSS, LICENSE and NOTICE already in this directory matched that archive byte for byte; the missing bundle license notice was copied from the same archive without changing the assets or package version.
