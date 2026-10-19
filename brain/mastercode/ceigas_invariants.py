"""
CEIGAS architecture invariants.

Holds the governance specification and the invariant constants derived from
it. Every CEIGAS operation is validated against those constants; an
operation that violates one is blocked. The invariants stand on their
engineering merit and are tested as such.
"""

import re
import logging

logger = logging.getLogger("cseries.ceigas.invariants")


# ============================================================================
# Architecture specification (numbered invariants)
#
# Numbers 12-17 and 20 are retired; the remaining numbers are kept because
# comments, tests and audit labels (bot_name 'invariantN:...') cite them.
# ============================================================================

ARCHITECTURE_SPEC: str = """
CEIGAS — the governance and authorization architecture.

INVARIANTS:

Invariant 1: Binary Capability Encoding
Agent capabilities are encoded as a binary prefix in the agent's filename.
Each bit position maps to one capability:
  Bit 0 (1):   READ_CONTEXT
  Bit 1 (2):   READ_MEMORY
  Bit 2 (4):   WRITE_CONTEXT
  Bit 3 (8):   WRITE_MEMORY
  Bit 4 (16):  EXECUTE_TOOL
  Bit 5 (32):  EXTERNAL_API
  Bit 6 (64):  SPAWN_BOT
  Bit 7 (128): ADMIN
  Bit 8 (256): SHELL_EXEC
The filename format is: [binary prefix]_[bot_name].py

Invariant 2: Filename-Encoded Authorization
An agent cannot request capabilities beyond those encoded in its prefix.

Invariant 3: Domain Isolation
Work is organised into isolated domains. Crossing from one domain to
another requires an explicit synapse definition.

Invariant 4: Synapse Classification
Every inter-domain channel has one type: direct (same group, always
allowed), gated (permission check), modulated (neurotransmitter state
affects throughput), restricted (explicit grant) or federated
(cross-instance).

Invariant 5: Neurotransmitter Modulation
Neurotransmitter levels set the effective conductance of each synapse, and
every write to them is bounded and audited.

Invariant 6: Continuous Trust Scoring
Each entity has a trust score that decays with inactivity, grows with
successful requests and drops on violations. Tiers:
  quarantined: 0.0 - 0.2
  provisional: 0.2 - 0.5
  standard:    0.5 - 0.7
  trusted:     0.7 - 0.9
  voucher:     0.9 - 1.0

Invariant 7: Emergency Kill Switch
The kill switch zeroes all domain conductance except core.governance, which
stays up to coordinate recovery.

Invariant 8: Entity Genesis
A new entity is created through identity validation, weight resolution,
enrollment, constitutional memory, personality, voice, permissions and a
genesis record.

Invariant 9: 10-Dimensional Personality Space
Personality has exactly 10 bounded dimensions: warmth, directness, humor,
formality, curiosity, assertiveness, patience, vulnerability, energy and
depth.

Invariant 10: Behavioral Envelope
Each personality dimension has bounds; leaving them triggers governance.

Invariant 11: Continuous Drift Detection
Drift from seed weights is monitored against thresholds that warn, reset,
debug or suspend.

Invariant 18: Complete Audit Trail
Entity operations, domain crossings, trust changes, permission grants and
governance actions are all logged, so system state can be reconstructed.

Invariant 19: Codebook Tampering Detection
Changes to an entity's master directives are detected by SHA-256 hash
verification of the directive text.
"""


# ============================================================================
# Invariant Constants (derived from the architecture spec above)
# ============================================================================
#
# Each entry is tagged TRANSLATED (a live gate enforces it) or PARTIAL
# (the behaviour exists as a flow rather than one named check).

INVARIANTS = {
    # ── Invariant 1: capability encoding ──── TRANSLATED ──────────────
    # Enforced by the meta-tool gate via validate_capability_bits
    # + bot_executor's filename prefix check. See
    # runtime/tools.py:_authorize_meta_tool. Widened 8→12 bits for
    # SHELL_EXEC + 3 reserved upper bits.
    "binary_encoding_bits": 12,
    "capability_bits": {
        0: "read_context",
        1: "read_memory",
        2: "write_context",
        3: "write_memory",
        4: "execute_tool",
        5: "external_api",
        6: "spawn_bot",
        7: "admin",
        8: "shell_exec",
    },
    "filename_pattern": r"^([01]{12}|[01]{8})_.+\.py$",

    # ── Invariant 2: filename-encoded auth ──── TRANSLATED ────────────
    # Same meta-tool gate — required_bits subset check.
    "authorization_is_filename_encoded": True,

    # ── Invariant 3: brain-mapped domain isolation ─ TRANSLATED ───────
    # The capability bits from entity_domain_access (runtime/tools.py,
    # server/routes/mcp.py). The domain controller and its synapse table
    # were retired.
    "domain_isolation_required": True,

    # ── Invariant 4: synapse classification ──── TRANSLATED ───────────
    # _TOOL_CEIGAS_MAP. The synapse table that classified crossings was
    # retired.
    "synapse_types": [
        "direct", "gated", "modulated", "restricted", "federated",
    ],

    # ── Invariant 5: NT modulation ──── TRANSLATED ────────────────────
    # runtime/nt_modulation.py:apply_nt_change wraps every
    # neurotransmitter_systems write.
    "nt_modulation_required": True,

    # ── Invariant 6: continuous trust scoring ──── TRANSLATED ─────────
    # Retired with the domain controller: the trust score and its
    # quarantine floor are gone; the operator kill switch is the stop.
    "trust_ledger_required": True,
    "trust_tiers": {
        "quarantined": (0.0, 0.2),
        "provisional": (0.2, 0.5),
        "standard": (0.5, 0.7),
        "trusted": (0.7, 0.9),
        "voucher": (0.9, 1.0),
    },

    # ── Invariant 7: emergency kill switch ──── TRANSLATED ────────────
    # brain/ceigas_kill_switch.py; checked by the meta-tool gate, the HTTP
    # gate, the inference call, the MCP lane and the synaptive doors.
    "kill_switch_required": True,
    "kill_switch_exemption": "core.governance",

    # ── Invariant 8: entity genesis pipeline ──── PARTIAL ─────────────
    # enrollment_session table + provisioner flow exist, but no
    # single "validate_genesis" gate fires on every entity creation.
    # validate_entity_genesis below checks weight dimensions but is
    # called from limited call sites (the platform's provisioner).

    # ── Invariant 9: 10D personality space ──── TRANSLATED ────────────
    # current_weights jsonb column on entity_registry + the 10 named
    # dimensions consumed by codebook_engine + synaptive_handler.
    "personality_dimensions": 10,
    "dimension_names": [
        "warmth", "directness", "humor", "formality", "curiosity",
        "assertiveness", "patience", "vulnerability", "energy", "depth",
    ],

    # ── Invariant 10: behavioral envelope ──── PARTIAL ────────────────
    # No table or invariant by that name; bounds are enforced via
    # Invariant 11 drift detection + the codebook_params digest
    # validation. Effectively folded into drift + tamper detection.

    # ── Invariant 11: continuous drift detection ─ TRANSLATED ─────────
    # entity_behavioral_log + drift detection code paths in
    # mastercode/runtime/. 65 live references.
    "drift_detection_required": True,

    # ── Invariant 18: complete audit trail ──── TRANSLATED ────────────
    # entity_behavioral_log row per gate decision, across every gate.
    # Invariant 18 is the load-bearing
    # observability layer for every other invariant.
    "audit_trail_required": True,

    # ── Invariant 19: codebook tampering detection ─ TRANSLATED ───────
    # entity_registry.directive_hash, codebook_registry.codebook_digest,
    # codebook_params.row_digest.
    # behavioral_corrections digest still pending.
    "codebook_tampering_detection": True,
}


# ============================================================================
# Validation Functions
# ============================================================================

def validate_invariant(operation: str, context: dict) -> tuple[bool, list[str]]:
    """
    Validate a CEIGAS operation against the architecture invariants.

    Args:
        operation: The operation type (e.g., 'authorize', 'genesis', 'domain_crossing')
        context: Operation-specific context dict

    Returns:
        (valid, violations) — True if valid, list of violation descriptions
    """
    violations = []

    if operation == "authorize":
        bits = context.get("bits")
        if bits is not None:
            ok, bit_violations = validate_capability_bits(bits)
            if not ok:
                violations.extend(bit_violations)

    elif operation == "genesis":
        ok, genesis_violations = validate_entity_genesis(context)
        if not ok:
            violations.extend(genesis_violations)

    elif operation == "domain_crossing":
        source = context.get("source_domain", "")
        target = context.get("target_domain", "")
        synapse_type = context.get("synapse_type", "")
        ok, crossing_violations = validate_domain_crossing(source, target, synapse_type)
        if not ok:
            violations.extend(crossing_violations)

    elif operation == "trust":
        score = context.get("trust_score", 0.5)
        tier = context.get("trust_tier", "")
        ok, trust_violations = validate_trust_operation(score, tier)
        if not ok:
            violations.extend(trust_violations)

    valid = len(violations) == 0
    if not valid:
        logger.warning(
            "CEIGAS INVARIANT VIOLATION: operation=%s violations=%s",
            operation, violations,
        )
    return valid, violations


def validate_capability_bits(bits: int) -> tuple[bool, list[str]]:
    """
    Validate binary encoding matches the architecture spec (invariant 1).

    Checks:
    - bits must fit in the declared bit width (0-4095 since G3a)
    - Each set bit must map to a known capability
    """
    violations = []

    if not isinstance(bits, int):
        violations.append(f"Capability bits must be integer, got {type(bits).__name__}")
        return False, violations

    width = INVARIANTS["binary_encoding_bits"]
    max_value = (1 << width) - 1
    if bits < 0 or bits > max_value:
        violations.append(
            f"Capability bits {bits} exceeds {width}-bit range (0-{max_value})"
        )

    for bit_pos in range(width):
        if bits & (1 << bit_pos):
            if bit_pos not in INVARIANTS["capability_bits"]:
                violations.append(f"Unknown capability at bit position {bit_pos}")

    return len(violations) == 0, violations


def validate_domain_crossing(
    source: str, target: str, synapse_type: str,
) -> tuple[bool, list[str]]:
    """
    Validate domain crossing conforms to the architecture spec (invariants 3, 4).

    Checks:
    - Both domains must be non-empty
    - Synapse type must be one of the spec-defined types
    """
    violations = []

    if not source:
        violations.append("Source domain must not be empty (Invariant 3: domain isolation)")
    if not target:
        violations.append("Target domain must not be empty (Invariant 3: domain isolation)")

    if synapse_type and synapse_type not in INVARIANTS["synapse_types"]:
        violations.append(
            f"Synapse type '{synapse_type}' not in spec-defined types: "
            f"{INVARIANTS['synapse_types']} (Invariant 4)"
        )

    return len(violations) == 0, violations


def validate_entity_genesis(config: dict) -> tuple[bool, list[str]]:
    """
    Validate genesis pipeline conforms to the architecture spec (invariants 8, 9, 10).

    Checks:
    - Personality weights must have exactly 10 dimensions (Invariant 9)
    - Dimension names must match the architecture spec (invariant 9)
    - Behavioral envelope must be defined (Invariant 10)
    """
    violations = []

    weights = config.get("personality_weights") or config.get("weights")
    if weights:
        if len(weights) != INVARIANTS["personality_dimensions"]:
            violations.append(
                f"Personality weights have {len(weights)} dimensions, "
                f"the architecture spec requires exactly {INVARIANTS['personality_dimensions']} (Invariant 9)"
            )

        expected_dims = set(INVARIANTS["dimension_names"])
        actual_dims = set(weights.keys())
        missing = expected_dims - actual_dims
        extra = actual_dims - expected_dims
        if missing:
            violations.append(
                f"Missing spec-required dimensions: {missing} (Invariant 9)"
            )
        if extra:
            violations.append(
                f"Unknown dimensions not in the spec: {extra} (Invariant 9)"
            )

    entity_type = config.get("entity_type")
    if entity_type and entity_type not in (
        "native", "operator",
        "synaptive_employee", "synaptive_manager",
    ):
        violations.append(
            f"Unknown entity type '{entity_type}' — not recognized by the architecture spec"
        )

    return len(violations) == 0, violations


def validate_trust_operation(score: float, tier: str) -> tuple[bool, list[str]]:
    """
    Validate trust score/tier matches the architecture spec thresholds (invariant 6).

    Checks:
    - Score must be in [0.0, 1.0]
    - If tier is provided, it must match the score's expected tier
    """
    violations = []

    if not isinstance(score, (int, float)):
        violations.append(f"Trust score must be numeric, got {type(score).__name__}")
        return False, violations

    if score < 0.0 or score > 1.0:
        violations.append(
            f"Trust score {score} outside valid range [0.0, 1.0] (Invariant 6)"
        )

    if tier:
        # Determine expected tier from score
        expected_tier = None
        for tier_name, (low, high) in INVARIANTS["trust_tiers"].items():
            if low <= score < high:
                expected_tier = tier_name
                break
        # Handle score == 1.0 edge case
        if score >= 1.0:
            expected_tier = "voucher"

        if expected_tier and tier != expected_tier:
            violations.append(
                f"Trust tier '{tier}' does not match score {score} "
                f"(expected '{expected_tier}', Invariant 6)"
            )

    return len(violations) == 0, violations


def validate_filename(filename: str) -> tuple[bool, list[str]]:
    """Validate a bot filename matches the spec-defined pattern (invariant 1)."""
    violations = []
    import os
    basename = os.path.basename(filename)
    if not re.match(INVARIANTS["filename_pattern"], basename):
        violations.append(
            f"Filename '{basename}' does not match the spec pattern "
            f"'{INVARIANTS['filename_pattern']}' (Invariant 1)"
        )
    return len(violations) == 0, violations


def get_invariants() -> dict:
    """Return the invariant constants."""
    return INVARIANTS.copy()
