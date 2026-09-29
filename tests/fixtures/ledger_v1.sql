BEGIN TRANSACTION;
CREATE TABLE records (
        kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
        content_hash TEXT NOT NULL, recorded_at TEXT NOT NULL,
        PRIMARY KEY (kind, id)
    );
INSERT INTO "records" VALUES('disclosures','sec-accession-001','{"first_seen_at":"2026-01-05T20:05:00Z","id":"sec-accession-001","mode":"historical","published_at":"2026-01-05T20:00:00Z","source_url":"https://www.sec.gov/Archives/example.htm","symbol":"ABC","text":"The company disclosed a new material agreement."}','fe6952994fbdefe6c3e98744fcec0062a48645692f1d41e20687763567492b40','2026-01-05T20:05:00.000000Z');
INSERT INTO "records" VALUES('bars','ABC:2026-01-06','{"close":101.5,"open":100.0,"session":"2026-01-06","symbol":"ABC"}','e0d88f076d786013db0367bbdf580a97d4e2d3a4dd770d544a11491dc3d2463b','2026-01-06T21:05:00.000000Z');
INSERT INTO "records" VALUES('forecasts','f-001','{"direction":"up","event_id":"sec-accession-001","score":0.25}','2d06790f4f6c7626885795e0983e32089d7c733321e073790a861a2e1ee37d65','2026-01-06T21:10:00.000000Z');
INSERT INTO "records" VALUES('runs','r-001','{"job":"poll","note":"synthetic","status":"ok"}','0e077b98c130429d21417ff2434be169274d271e219c0496036e7aeb75e61772','2026-01-07T00:00:00Z');
CREATE TRIGGER records_no_update BEFORE UPDATE ON records
    BEGIN SELECT RAISE(ABORT, 'Ledger records are immutable'); END;
CREATE TRIGGER records_no_delete BEFORE DELETE ON records
    BEGIN SELECT RAISE(ABORT, 'Ledger records are immutable'); END;
CREATE TRIGGER records_no_replace BEFORE INSERT ON records
    WHEN EXISTS (SELECT 1 FROM records WHERE kind=NEW.kind AND id=NEW.id)
    BEGIN SELECT RAISE(ABORT, 'Ledger records are immutable'); END;
COMMIT;
PRAGMA user_version=1;
