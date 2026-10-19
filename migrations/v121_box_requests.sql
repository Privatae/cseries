-- v121 (2026-08-29): approval requests pending ON THE USER'S MACHINE.
--
-- When a bastion holds its own second factor (cseries-bastion stepup-enroll),
-- a store grant or a move plan for that machine is not approved with a code
-- typed into the pod: the pod files a REQUEST on the box (stepup_request —
-- purpose + the plain details the box verifies against it), the user reads it
-- and approves it on the machine (cseries-bastion approvals / approve), and the
-- pod completes the approval when the box reports it approved (stepup_status,
-- polled on the user's next click and on every heartbeat). The pod never
-- handles the code and never chooses the words the user confirms.
--
-- box_request: {"request": "<id>", "server": "<hostname>", "purpose": "<str>",
--               "at": "<iso>"} while pending; NULL otherwise.
-- The runtime ensures both columns defensively (nac_dcn.ensure_plans_table);
-- this is the authoritative record.
ALTER TABLE dcn_stores     ADD COLUMN IF NOT EXISTS box_request JSONB;
ALTER TABLE dcn_move_plans ADD COLUMN IF NOT EXISTS box_request JSONB;
