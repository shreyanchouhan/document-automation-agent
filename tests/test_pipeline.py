from docagent.routing import AUTO_POST, NEEDS_REVIEW, REJECTED


def test_every_document_lands_in_exactly_one_lane(processed_pipeline):
    store = processed_pipeline.store
    summary = store.batch_summary()
    assert summary["documents"] == len(processed_pipeline.manifest.entries)
    assert summary["auto_post"] + summary["needs_review"] + summary["rejected"] == summary["documents"]
    assert summary["auto_post"] > 0 and summary["needs_review"] > 0


def test_auto_posted_documents_reach_the_ledger(processed_pipeline):
    store = processed_pipeline.store
    posted = store.connection.execute(
        "SELECT COUNT(*) FROM postings WHERE source = 'auto'"
    ).fetchone()[0]
    auto = store.batch_summary()["auto_post"]
    assert posted == auto


def test_review_queue_holds_exactly_the_flagged_documents(processed_pipeline):
    store = processed_pipeline.store
    queued = store.connection.execute(
        "SELECT COUNT(*) FROM review_queue WHERE status = 'open'"
    ).fetchone()[0]
    assert queued == store.batch_summary()["needs_review"]


def test_every_queued_document_carries_a_reason(processed_pipeline):
    for review in processed_pipeline.store.open_reviews(limit=20):
        assert review["reasons"], review["document_id"]


def test_a_correction_posts_the_invoice_and_records_the_effort(processed_pipeline):
    store = processed_pipeline.store
    review = store.open_reviews(limit=1)[0]
    document_id = review["document_id"]

    processed_pipeline.apply_review(
        document_id=document_id,
        resolution="corrected",
        reviewer="tester",
        corrections={"total_amount": 1234.56},
        seconds_spent=42.0,
    )

    payload = store.extraction_payload(document_id)
    assert payload["fields"]["total_amount"]["value"] == 1234.56
    assert payload["fields"]["total_amount"]["confidence"] == 1.0

    posting = store.connection.execute(
        "SELECT * FROM postings WHERE document_id = ? AND source = 'human'", (document_id,)
    ).fetchone()
    assert posting["total_amount"] == 1234.56
    assert store.review_effort()["seconds"] >= 42.0


def test_reprocessing_a_batch_is_idempotent(pipeline):
    first = pipeline.process_batch(pipeline.manifest.batch_ids()[0])
    second = pipeline.process_batch(pipeline.manifest.batch_ids()[0])
    assert [record.action for record in first] == [record.action for record in second]


def test_rejections_are_confident_rejections(processed_pipeline):
    store = processed_pipeline.store
    rows = store.connection.execute(
        "SELECT * FROM decisions WHERE action = ?", (REJECTED,)
    ).fetchall()
    for row in rows:
        assert row["minimum_critical_confidence"] >= processed_pipeline.config.critical_confidence_floor
