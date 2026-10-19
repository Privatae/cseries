"""Groups — a roster and a signed vote.

Two tiers share one shape. This package is the CREATOR-HELD tier: a group
circle whose roster lives in its creator's pod and is replicated to members
over the direct host<->host peer lane. The platform-held tier (communityd
groups, where the platform assigns moderators) has the same shape with a
different owner and lives on the platform.

  votes.py   the signing scheme — what bytes are signed, with which key, and
             how a holder of the roster recounts a tally.
  roster.py  the roster itself: create, add, remove, roles, epochs, and the
             replica a member keeps of someone else's group.
  lane.py    the three peer ops that replicate a roster and carry a vote, and
             the store-and-forward queue behind them.

The group app grant lives in server/peer/app_lane.py beside the per-contact
grant it generalises, not here — it is the app lane's own credential with a
group-scoped resource, and forking it would have been the worse half of reuse.
"""
