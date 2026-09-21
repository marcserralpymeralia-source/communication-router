# Multi-destination cases

Explicit human multi-destination corrections are analyzed individually. The second destination is not a runner-up by default. All addresses and message text below are synthetic fixtures; domains preserve the `ingesco` versus `quibac` boundary without being deliverable.

## Case 107

- Sender: `sender-107@source.test`; subject: `(none)`.
- Body summary: Se solicita adelantar una inspección y aportar una copia de una factura para contabilidad.
- Legacy: `operaciones@ingesco.test`; correction: `operaciones@ingesco.test + facturacion@ingesco.test`.
- Destinations: `operaciones@ingesco.test`, `facturacion@ingesco.test`.
- Classification: **MULTI-ACTION REAL**.
- Recommendation: Mantener dos acciones explícitas en la evaluación; no usar el segundo destino como runner-up.

## Case 108

- Sender: `sender-108@source.test`; subject: `(none)`.
- Body summary: Se solicita adelantar una inspección y aportar una copia de una factura para contabilidad.
- Legacy: `operaciones@ingesco.test`; correction: `operaciones@ingesco.test + facturacion@ingesco.test`.
- Destinations: `operaciones@ingesco.test`, `facturacion@ingesco.test`.
- Classification: **MULTI-ACTION REAL**.
- Recommendation: Mantener dos acciones explícitas en la evaluación; no usar el segundo destino como runner-up.

## Case 125

- Sender: `sender-125@source.test`; subject: `(none)`.
- Body summary: Se solicita adelantar una inspección y revisar un plan de seguridad y salud adjunto.
- Legacy: `operaciones@ingesco.test`; correction: `también a prevencion@ingesco.test`.
- Destinations: `operaciones@ingesco.test`, `prevencion@ingesco.test`.
- Classification: **CC/NOTIFICACIÓN**.
- Recommendation: Operaciones es responsable de la coordinación; Prevención debe tratarse como informado/participante por el plan de seguridad.
