-- v190 (2026-09-28): a person's "no" to a bastion update, remembered by the Host.
--
-- A bastion update that still needs an approval (a bastion older than 0.44.0,
-- or a move to an older version) asks the person's approval device directly,
-- with no card in front of it (nac_approval._ask_directive). The card used to
-- be where a refusal was kept. The device keeps its own record only until 24
-- hours after the ask was filed (bastion device.go), and automatic updates
-- queue the published version again every 10 minutes, so without a record
-- here the person would be asked again within a day of saying no.
--
-- Keyed by the pairing (token), as the cards were. A no stands for its version
-- once the Host has seen it (at the machine's next check-in), whichever way the
-- target moves later; a newer version is a new question; a re-paired machine is
-- a new pairing and is asked again. Rows are written only by the person's own
-- refusals, a handful over a machine's life, so none are deleted.
CREATE TABLE IF NOT EXISTS bastion_update_refusals (
    token_id   INTEGER NOT NULL,
    version    TEXT NOT NULL,
    refused_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (token_id, version)
);
