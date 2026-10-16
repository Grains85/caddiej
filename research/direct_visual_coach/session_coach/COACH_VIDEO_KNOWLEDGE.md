# Coach-video knowledge

This is a place to store what a coach said. It is not a coaching rulebook,
and this repository does not download or watch videos.

## Later workflow

1. A person points at one coaching video.
2. A transcript is stored with timestamps.
3. A person, or a later extractor, writes a knowledge record: what was
   observed, a possible cause, a suggested correction, and a suggested drill.
4. The record stays `unreviewed`.
5. A person marks it `approved` or `rejected`, and writes down contradictions
   with other records.
6. An approved record may be copied, by hand, into a new version of
   `CoachingRulebook`.

Nothing in that chain promotes a record by itself. Two coaches who disagree
both stay in the store. The rulebook version that the session engine loads
is a separate file with its own version. Building that file from real
coaching video is a later project. The session loop uses the synthetic
fixture until a reviewed version exists.

## Record

`KnowledgeRecord` holds the source video, coach identity, start and end
times, the original statement, the extracted observation, a possible cause,
a suggested correction, a suggested drill, swing stages, an uncertainty
note, a review status, and contradiction ids.

A possible cause is not an observation. The uncertainty note has to say
`unverified` when a cause is present. `draft_from_approved` returns a short
note with `approval_status` `awaiting_rulebook_edit`, or nothing. That note
is not loaded by `load_rulebook`.

## What is not here

No video files, no transcripts, no extractor, and no merge of contradictory
advice.
