"""Independent identity gates for offline developer-to-map review."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from sources.property import developer_matching as matching


def address(*, city: str = "Москва", municipality: str = "Даниловский",
            street: str = "Автозаводская улица", house: str = "23",
            corpus: str | None = "5", structure: str | None = None,
            plot: str | None = None, quality: str = "exact") -> dict:
    return {"city": city, "municipality": municipality, "street": street,
            "house": house, "corpus": corpus, "structure": structure,
            "plot": plot, "parse_quality": quality}


def unit(*, source_group: str = "etalon", project_id: str | None = "shagal-1",
         building_id: str = "stage5-corpus5", unit_id: str = "unit-241") -> dict:
    return {"source_group": source_group,
            "source_url": "https://etalongroup.ru/msk/choose/flat/shagal/101771/",
            "observation_id": "obs-101771",
            "project": {"publisher_project_id": project_id, "name": "Шагал"},
            "building": {"publisher_building_id": building_id,
                         "address_components": address()},
            "unit": {"publisher_unit_id": unit_id, "number": 241},
            "identity_quality": "publisher_asserted", "identity_conflict": False,
            "source_fields": {"location_scope": "in_scope_moscow"}}


def target(*, point_id: str = "mappt_" + "a" * 24,
           corpus: str | None = "5", city: str = "Москва") -> dict:
    return {"map_point_id": point_id, "entity_scope": "mapped_building",
            "address_components": address(corpus=corpus, city=city),
            "address_parse_quality": "exact_source_tag_presence",
            "visibility": "mapped_candidate", "record_kind": "mapped_construction_candidate",
            "name": "ЖК Шагал",
            "source_url": "https://www.openstreetmap.org/way/123"}


PROVIDERS = {"etalon": {"allowed_hosts": ["etalongroup.ru"]}}


def test_unit_to_publisher_jk_and_unique_corpus_to_map_are_distinct_edges() -> None:
    result = matching.review_matches([unit()], [], [target()], PROVIDERS)
    assert result["units"][0]["decision"] == "accepted"
    assert result["units"][0]["map_target_id"] is None
    assert result["building_matches"][0]["decision"] == "accepted"
    assert result["building_matches"][0]["reason_code"] == "full_building_address_exact"
    assert result["groups"][0]["accepted_local_target_ids"] == [target()["map_point_id"]]


def test_name_or_publisher_alone_does_not_join_mapped_building() -> None:
    row = unit()
    row["building"] = {"publisher_building_id": "stage5-corpus5"}
    result = matching.review_matches([row], [], [target()], PROVIDERS)
    assert result["units"][0]["decision"] == "accepted"
    assert result["building_matches"][0]["decision"] == "unmatched"
    assert result["groups"][0]["accepted_local_target_ids"] == []


def test_corpus_and_structure_are_not_interchangeable_or_omitted() -> None:
    base = unit()["building"]
    assert matching.compare_building_target(base, target(corpus="6")).reason_code == "corpus_conflict"
    assert matching.compare_building_target(base, target(corpus=None)).reason_code == (
        "corpus_or_structure_missing")
    target_with_structure = target(corpus=None)
    target_with_structure["address_components"]["structure"] = "5"
    assert matching.compare_building_target(base, target_with_structure).decision == "review"


def test_compound_house_suffix_is_a_review_alias_not_an_exact_corpus() -> None:
    local = target(corpus=None)
    local["address_components"]["house"] = "23к5"
    assert matching.compare_building_target(unit()["building"], local).reason_code == (
        "combined_house_corpus_not_verified")


def test_plot_alias_and_untyped_address_stay_in_review() -> None:
    building = unit()["building"]
    local = target()
    building["address_components"]["plot"] = "23/75"
    assert matching.compare_building_target(building, local).reason_code == (
        "plot_vs_building_address")
    building["address_components"]["plot"] = None
    building["address_components"]["parse_quality"] = "partial"
    assert matching.compare_building_target(building, local).reason_code == "address_not_exact"


def test_municipality_and_city_conflicts_reject_candidate_pair() -> None:
    building = unit()["building"]
    assert matching.compare_building_target(building, target(city="Мытищи")).reason_code == (
        "city_conflict")
    local = target()
    local["address_components"]["municipality"] = "Троицк"
    assert matching.compare_building_target(building, local).reason_code == (
        "municipality_conflict")


def test_ambiguous_same_full_address_never_auto_accepts() -> None:
    second = target(point_id="mappt_" + "b" * 24)
    result = matching.review_matches([unit()], [], [target(), second], PROVIDERS)
    assert {row["decision"] for row in result["building_matches"]} == {"review"}
    assert {row["reason_code"] for row in result["building_matches"]} == {
        "multiple_local_targets"}


def test_official_building_id_can_link_when_scope_is_building() -> None:
    building = unit()["building"]
    building.pop("address_components")
    building["official_ids"] = [{"namespace": "permit-building", "value": "77-123",
                                  "scope": "building"}]
    local = target()
    local["official_ids"] = building["official_ids"]
    assert matching.compare_building_target(building, local).reason_code == (
        "official_building_id_exact")
    local["entity_scope"] = "project"
    assert matching.compare_building_target(building, local).decision == "review"


def test_same_publisher_project_id_in_different_sources_is_separate() -> None:
    first = matching.group_id("etalon", "p-1")
    second = matching.group_id("brusnika", "p-1")
    assert first != second


def test_host_allowlist_rejects_credentials_and_nonofficial_host() -> None:
    for url in ("https://evil.example/flat/1", "https://user@etalongroup.ru/flat/1",
                "http://etalongroup.ru/flat/1"):
        row = unit()
        row["source_url"] = url
        try:
            matching.review_matches([row], [], [], PROVIDERS)
        except ValueError:
            pass
        else:
            raise AssertionError(f"unsafe URL accepted: {url}")


def test_html_escapes_source_values_and_csv_neutralizes_formulas() -> None:
    row = {"source_group": '<script>alert(1)</script>',
           "publisher_project_id": '=HYPERLINK("x")', "building_ref": "devbld_a",
           "target_id": None, "decision": "review", "reason_code": "address_not_exact"}
    body = matching.review_html([row])
    assert "<script>alert(1)</script>" not in body
    assert "\\u003cscript\\u003ealert(1)" in body
    assert 'body.replaceChildren()' in body
    assert matching._csv_cell('=HYPERLINK("x")').startswith("'")


def test_offline_review_indexes_rows_without_rendering_large_static_tables() -> None:
    row = {"source_group": "a101", "publisher_project_id": "project",
           "building_ref": "corpus", "target_id": "estateline:1",
           "decision": "review", "reason_code": "address_not_exact"}
    body = matching.review_html([row] * 300, units=[row] * 300)
    assert body.count("<tr>") == 3  # Headers only; JavaScript renders the current page.
    assert "На странице показывается не более 100 строк" in body
    assert "page*size,(page+1)*size" in body
    assert "window.innerWidth<=600?25:100" in body


def test_real_target_quality_and_holding_do_not_auto_join() -> None:
    local = target()
    local["address_components"]["house"] = "вл23"
    local["address_parse_quality"] = "exact_source_tag_presence"
    building = unit()["building"]
    building["address_components"]["house"] = "вл23"
    assert matching.compare_building_target(building, local).reason_code == (
        "holding_not_finished_house")
    local["address_components"]["house"] = "23"
    local["entity_scope"] = "registry_record"
    assert matching.compare_building_target(building, local).decision != "accepted"


def test_page_local_conflict_and_unknown_scope_never_group_unit() -> None:
    for change, reason in (
            ({"identity_quality": "page_local"}, "publisher_unit_identity_unverified"),
            ({"identity_conflict": True}, "publisher_identity_conflict"),
            ({"location_scope": "unknown", "source_fields": {}}, "moscow_scope_unverified")):
        row = unit()
        row.update(change)
        result = matching.review_matches([row], [], [target()], PROVIDERS)
        assert result["units"][0]["decision"] == "review"
        assert result["units"][0]["reason_code"] == reason
        assert not result["groups"]


def test_flat_project_projection_does_not_claim_building() -> None:
    row = {"source_group": "etalon", "source_url": unit()["source_url"],
           "observation_id": "project-1", "record_type": "project",
           "publisher_project_id": "shagal-1", "project_name": "Шагал",
           "identity_quality": "publisher_asserted",
           "address_components": address(), "location_scope": "in_scope_moscow"}
    result = matching.review_matches([], [row], [target()], PROVIDERS)
    assert result["building_matches"] == []
    assert result["project_matches"][0]["decision"] == "accepted"
    assert result["project_matches"][0]["geometry_claimed"] is False
    assert len(result["groups"]) == 1
    assert result["groups"][0]["map_link_status"] == "project_address_only"


def test_unique_house_only_project_address_can_link_registry_project() -> None:
    project = {"source_group": "etalon", "source_url": unit()["source_url"],
               "observation_id": "project-2", "record_type": "project",
               "publisher_project_id": "house-project", "project_name": "ЖК Север",
               "identity_quality": "publisher_asserted", "location_scope": "in_scope_moscow",
               "address_components": address(corpus=None)}
    local = target(corpus=None)
    local["entity_scope"] = "registry_record"
    local["address_parse_quality"] = "exact_street_house_only"
    result = matching.review_matches([], [project], [local], PROVIDERS)
    assert result["project_matches"][0]["decision"] == "accepted"
    assert result["project_matches"][0]["association_scope"] == "project_address_only"
    assert result["building_matches"] == []
    assert result["groups"][0]["accepted_project_target_ids"] == [local["map_point_id"]]
    assert matching.search_queue([local], {result["project_matches"][0]["target_id"]})[
        "counts"]["active_address_tasks"] == 0


def test_project_address_ambiguity_and_aggregator_do_not_auto_link() -> None:
    project = {"source_group": "etalon", "source_url": unit()["source_url"],
               "observation_id": "project-3", "record_type": "project",
               "publisher_project_id": "ambiguous", "project_name": "ЖК Север",
               "identity_quality": "publisher_asserted", "location_scope": "in_scope_moscow",
               "address_components": address(corpus=None)}
    local = target(corpus=None)
    duplicate = target(point_id="mappt_" + "d" * 24, corpus=None)
    result = matching.review_matches([], [project], [local, duplicate], PROVIDERS)
    assert {item["decision"] for item in result["project_matches"]} == {"review"}
    project["source_group"] = "proplanirovki"
    project["source_url"] = "https://proplanirovki.ru/object/1"
    aggregator = matching.review_matches([], [project], [local], {
        "proplanirovki": {"allowed_hosts": ["proplanirovki.ru"]}})
    assert aggregator["project_matches"][0]["reason_code"] == "third_party_project_address"
    third_party_unit = unit(source_group="proplanirovki")
    third_party_unit["source_url"] = "https://proplanirovki.ru/object/1"
    third_party_building = matching.review_matches([third_party_unit], [], [target()], {
        "proplanirovki": {"allowed_hosts": ["proplanirovki.ru"]}})
    assert third_party_building["building_matches"][0]["reason_code"] == (
        "third_party_building_address")


def test_page_local_project_identity_does_not_found_group() -> None:
    row = {"source_group": "etalon", "source_url": unit()["source_url"],
           "observation_id": "project-1", "record_type": "project",
           "publisher_project_id": "page-local", "project_name": "Шагал",
           "identity_quality": "page_local", "location_scope": "in_scope_moscow"}
    result = matching.review_matches([], [row], [], PROVIDERS)
    assert result["projects"][0]["reason_code"] == "publisher_project_identity_unverified"
    assert result["groups"] == []


def test_unknown_scope_source_project_keeps_identity_without_offer_claim() -> None:
    project = {"source_group": "etalon", "source_url": unit()["source_url"],
               "observation_id": "project-unknown", "record_type": "project",
               "publisher_project_id": "source-project-1", "project_name": "ЖК Север",
               "identity_quality": "publisher_asserted", "location_scope": "unknown"}
    result = matching.review_matches([], [project], [target()], PROVIDERS)
    assert result["projects"][0]["decision"] == "review"
    assert result["projects"][0]["source_location_scope"] == "unknown"
    assert result["groups"][0]["source_location_scopes"] == ["unknown"]
    assert result["groups"][0]["publisher_unit_ids"] == []
    assert result["groups"][0]["map_link_status"] == "unlinked"


def test_agent_reviewed_official_cadastre_links_project_only(tmp_path) -> None:
    project = {"source_group": "a101", "source_url": "https://a101.ru/projects/prokshino/",
               "observation_id": "project-prokshino", "record_type": "project",
               "publisher_project_id": "prokshino", "project_name": "Прокшино",
               "identity_quality": "publisher_asserted", "location_scope": "in_scope_moscow"}
    local = target(point_id="mappt_" + "e" * 24)
    local["address"] = "Москва, Прокшинский проспект, участок 77:17:0120114:5803"
    observation = {"observation_id": "research-1",
                   "targets": [{"target_id": local["map_point_id"],
                                "assessment": "accept_candidate_project_identity"}],
                   "evidence": [{"source_url": "https://cdn.a101.ru/project.pdf",
                                 "claims": ["Прокшино строится на участке 77:17:0120114:5803"]}]}
    body = (json.dumps(observation, ensure_ascii=False) + "\n").encode()
    research = tmp_path / "evidence.jsonl"
    research.write_bytes(body)
    link = {"schema_version": "developer-project-adjudication/1",
            "reviewer_kind": "agent",
            "source_group": "a101", "publisher_project_id": "prokshino",
            "project_name": "Прокшино", "target_id": local["map_point_id"],
            "target_cadastre": "77:17:0120114:5803",
            "evidence_url": "https://cdn.a101.ru/project.pdf",
            "research_file": research.name,
            "research_sha256": hashlib.sha256(body).hexdigest(),
            "research_observation_id": "research-1"}
    result = matching.review_matches([], [project], [local], {
        "a101": {"allowed_hosts": ["a101.ru"]}})
    matching.apply_reviewed_project_links(result, [link], [project], [local], tmp_path)
    accepted = [row for row in result["project_matches"] if row["decision"] == "accepted"]
    assert len(accepted) == 1 and accepted[0]["geometry_claimed"] is False
    assert accepted[0]["reviewer_kind"] == "agent"
    assert result["groups"][0]["accepted_project_target_ids"] == [local["map_point_id"]]
    assert result["groups"][0]["map_link_status"] == "project_address_only"
    unknown_project = {**project, "location_scope": "unknown"}
    unknown_result = matching.review_matches([], [unknown_project], [local], {
        "a101": {"allowed_hosts": ["a101.ru"]}})
    matching.apply_reviewed_project_links(unknown_result, [link], [unknown_project],
                                          [local], tmp_path)
    assert unknown_result["groups"][0]["accepted_project_target_ids"] == [
        local["map_point_id"]]
    assert unknown_result["groups"][0]["source_location_scopes"] == ["unknown"]
    with pytest.raises(ValueError, match="agent review"):
        matching.apply_reviewed_project_links(result, [{**link, "reviewer_kind": "human_verified"}],
                                              [project], [local], tmp_path)
    with pytest.raises(ValueError, match="hash mismatch"):
        matching.apply_reviewed_project_links(result, [{**link, "research_sha256": "0" * 64}],
                                             [project], [local], tmp_path)


def test_agent_reviewed_full_official_site_links_project_only(tmp_path) -> None:
    project = {"source_group": "fsk", "source_url": "https://fsk.ru/amber-city",
               "observation_id": "official-project", "record_type": "project",
               "publisher_project_id": "amber", "project_name": "ЖК «Амбер Сити»",
               "address": "Москва, 3-й Хорошевский пр., вл. 3А",
               "identity_quality": "publisher_asserted", "location_scope": "in_scope_moscow"}
    local = target(point_id="mappt_" + "f" * 24)
    local["address"] = "г. Москва, 3-й Хорошёвский проезд, вл. 3А, участок 77:09:1:2"
    observation = {"observation_id": "site-review", "targets": [
        {"target_id": local["map_point_id"],
         "assessment": "accept_candidate_project_identity"}],
        "evidence": [{"source_url": "https://fsk.ru/amber-city", "claims": [
            "Официальный проект ЖК Амбер Сити: 3-й Хорошевский пр., вл. 3А"]}]}
    body = (json.dumps(observation, ensure_ascii=False) + "\n").encode()
    (tmp_path / "evidence.jsonl").write_bytes(body)
    link = {"schema_version": "developer-project-adjudication/1", "reviewer_kind": "agent",
            "source_group": "fsk", "publisher_project_id": "amber",
            "project_name": "ЖК «Амбер Сити»", "target_id": local["map_point_id"],
            "match_basis": "official_full_project_site_address",
            "source_address_fragment": "3-й Хорошевский пр., вл. 3А",
            "target_address_fragment": "3-й Хорошёвский проезд, вл. 3А",
            "evidence_url": "https://fsk.ru/amber-city", "research_file": "evidence.jsonl",
            "research_sha256": hashlib.sha256(body).hexdigest(),
            "research_observation_id": "site-review"}
    result = matching.review_matches([], [project], [local], {
        "fsk": {"allowed_hosts": ["fsk.ru"]}})
    matching.apply_reviewed_project_links(result, [link], [project], [local], tmp_path)
    assert result["groups"][0]["accepted_project_target_ids"] == [local["map_point_id"]]
    assert not result["groups"][0]["accepted_local_target_ids"]
    confirmed = matching.confirmed_local_complex_groups(result["groups"],
                                                        result["project_matches"], [local])
    assert confirmed[0]["local_target_ids"] == [local["map_point_id"]]
    assert confirmed[0]["local_members"][0]["corpus_geometry_status"] == "unresolved"
    assert confirmed[0]["cross_provider_union_verified"] is False
    with pytest.raises(ValueError, match="addresses differ"):
        matching.apply_reviewed_project_links(result, [{**link,
            "target_address_fragment": "3-й Хорошёвский проезд, вл. 5А"}],
            [project], [local], tmp_path)
    duplicate_site = {**project, "publisher_project_id": "other",
                      "project_name": "Другой ЖК"}
    with pytest.raises(ValueError, match="Another publisher project"):
        matching.apply_reviewed_project_links(result, [link], [project, duplicate_site],
                                              [local], tmp_path)


def test_agent_reviewed_aggregator_site_needs_primary_evidence_and_unique_project() -> None:
    local = target(point_id="mappt_" + "9" * 24)
    local["address"] = "Москва, улица Южная, дом 1"
    project = {"source_group": "proplanirovki", "source_url": "https://proplanirovki.ru/api/catalog/p1",
               "observation_id": "source-project-1", "record_type": "project",
               "publisher_project_id": "p1", "project_name": "Южные сады",
               "identity_quality": "publisher_asserted", "location_scope": "unknown"}
    other = {**project, "source_url": "https://proplanirovki.ru/api/catalog/p2",
             "observation_id": "source-project-2", "publisher_project_id": "p2",
             "project_name": "Другой ЖК"}
    providers = {"proplanirovki": {"allowed_hosts": ["proplanirovki.ru"]}}
    result = matching.review_matches([], [project, other], [local], providers)
    def reviewed(source: dict, *, evidence_url: str) -> dict:
        return {"schema_version": "developer-project-adjudication-result/1",
                "reviewer_kind": "agent", "decision": "accepted_project",
                "corpus_link_status": "unresolved", "geometry_status": "unresolved",
                "source_group": "proplanirovki",
                "publisher_project_id": source["publisher_project_id"],
                "publisher_project_name": source["project_name"],
                "publisher_source_url": source["source_url"],
                "source_observation_id": source["observation_id"],
                "target_id": local["map_point_id"], "target_address": local["address"],
                "reason_ru": "Агент проверил официальный адрес площадки, но корпус не установлен.",
                "evidence_urls": [evidence_url],
                "source_access": [{"url": evidence_url,
                                   "scope": "Официальный адрес площадки проекта"}],
                "checked_at": "2026-10-10T16:00:00Z"}
    official_url = "https://www.mos.ru/official-site.pdf"
    secondary_url = "https://www.realestate.ru/project"
    matching.apply_agent_site_adjudications(result, [
        (reviewed(project, evidence_url=official_url), "a" * 64),
        (reviewed(other, evidence_url=official_url), "a" * 64)],
        [project, other], [local])
    accepted = [row for row in result["project_matches"] if row["decision"] == "accepted"]
    assert len(accepted) == 1 and accepted[0]["publisher_project_id"] == "p1"
    assert any(row["reason_code"] == "multiple_publisher_projects"
               for row in result["project_matches"])
    secondary_result = matching.review_matches([], [project], [local], providers)
    matching.apply_agent_site_adjudications(secondary_result, [
        (reviewed(project, evidence_url=secondary_url), "b" * 64)], [project], [local])
    assert not any(row["decision"] == "accepted"
                   for row in secondary_result["project_matches"])


def test_search_queue_deduplicates_address_and_limits_consulted_pages() -> None:
    first = target(point_id="mappt_" + "a" * 24)
    second = target(point_id="mappt_" + "b" * 24)
    third = target(point_id="mappt_" + "c" * 24)
    third["name"] = "Детский сад"
    missing = target(point_id="mappt_" + "d" * 24)
    missing["address_components"]["house"] = None
    missing["name"] = "Корпус 12"
    queue = matching.search_queue([first, second, third, missing], set())
    assert queue["counts"] == {"address_tasks": 1, "first_batch": 1,
                               "active_address_tasks": 1, "deferred_archived_tasks": 0,
                               "insufficient_input": 2}
    item = queue["tasks"][0]
    assert item["target_ids"] == [first["map_point_id"], second["map_point_id"]]
    assert item["max_consulted_pages"] == 5 and item["consulted_pages"] == []
    assert item["remaining_pages"] == 5
    assert queue["first_batch"][0]["assigned_research_lane"] == "report_sources"


def test_claims_survive_ranked_three_worker_partition() -> None:
    rows = [target(point_id="mappt_" + character * 24)
            for character in "abcdef"]
    for index, row in enumerate(rows):
        row["address_components"]["house"] = str(10 + index)
    claims = {rows[0]["map_point_id"]: "luna_cache",
              rows[1]["map_point_id"]: "luna_clinics",
              rows[2]["map_point_id"]: "luna_coverage"}
    queue = matching.search_queue(rows, set(), first_batch=2, claims=claims)
    assigned = {item["target_ids"][0]: item["assigned_research_lane"]
                for item in queue["tasks"]}
    assert assigned[rows[0]["map_point_id"]] == "luna_cache"
    assert assigned[rows[1]["map_point_id"]] == "luna_clinics"
    assert assigned[rows[2]["map_point_id"]] == "luna_coverage"
    assert sum(len(rows) for rows in queue["shards"].values()) == 6
    assert all(len(rows) <= 2 for rows in queue["first_batches"].values())


def test_raw_plot_address_is_searchable_but_not_an_exact_map_join() -> None:
    local = target()
    local["address_components"] = {"city": "Москва", "street": None, "house": None,
                                   "corpus": None, "structure": None, "plot": None}
    local["address_parse_quality"] = "partial"
    local["address"] = "г. Москва, 119361, ул. Озёрная, вл. 44 (участок 33А/Б)"
    queue = matching.search_queue([local], set())
    assert queue["counts"]["active_address_tasks"] == 1
    assert queue["tasks"][0]["query_basis"] == "raw_source_address_search_only"
    assert "вл. 44" in queue["tasks"][0]["query"]
    assert matching.compare_building_target(unit()["building"], local).decision != "accepted"


def test_immutable_private_artifact_hashes_and_replay(tmp_path) -> None:
    run = tmp_path / "harvest"
    run.mkdir()
    unit_bytes = (json.dumps(unit(), ensure_ascii=False) + "\n").encode()
    project_bytes = b""
    (run / "developer_units.jsonl").write_bytes(unit_bytes)
    (run / "developer_projects.jsonl").write_bytes(project_bytes)
    manifest = {"schema_version": "developer-harvest-run/1", "providers": PROVIDERS,
                "files": {"developer_units": {"path": "developer_units.jsonl",
                                              "sha256": hashlib.sha256(unit_bytes).hexdigest(),
                                              "records": 1},
                          "developer_projects": {"path": "developer_projects.jsonl",
                                                 "sha256": hashlib.sha256(project_bytes).hexdigest(),
                                                 "records": 0}}}
    (run / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    targets = tmp_path / "targets.jsonl"
    targets.write_text(json.dumps(target()) + "\n", encoding="utf-8")
    output = tmp_path / "matching"
    result = matching.write_review(run / "manifest.json", targets, output)
    assert matching.write_review(run / "manifest.json", targets, output) == result
    receipt = json.loads((result / "manifest.json").read_text())
    schema = json.loads((Path(__file__).resolve().parents[3] /
                         "contracts/developer-matching.schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate(receipt)
    task_validator = Draft202012Validator(schema["$defs"]["search_task"])
    for line in (result / "search_tasks.jsonl").read_text().splitlines():
        task_validator.validate(json.loads(line))
    assert receipt["publication_status"] == "private_review_only"
    for name, pinned in receipt["files"].items():
        assert hashlib.sha256((result / name).read_bytes()).hexdigest() == pinned["sha256"]
    assert result.stat().st_mode & 0o077 == 0
    assert (result / "project_matches.tsv").exists()
    assert (result / "building_matches.tsv").exists()
    assert (result / "units.tsv").exists()
    assert "Связь проекта с адресом" in (result / "report.html").read_text()
    assert b"full_building_address_exact" in (result / "building_matches.jsonl").read_bytes()
    (run / "developer_units.jsonl").write_bytes(unit_bytes + unit_bytes)
    with pytest.raises(ValueError, match="hash mismatch"):
        matching.write_review(run / "manifest.json", targets, output)


def test_search_evidence_is_pinned_and_rejects_sixth_page(tmp_path) -> None:
    review = tmp_path / "review"
    review.mkdir()
    (review / "manifest.json").write_text(json.dumps({"run_id": "developer-review-test"}))
    task = {"address_key": "address_" + "a" * 24, "target_ids": ["estateline:1"],
            "status": "unsearched"}
    (review / "search_tasks.jsonl").write_text(json.dumps(task) + "\n")
    research = tmp_path / "research"
    research.mkdir()
    result = research / "observations.jsonl"
    row = {"address_key": task["address_key"], "target_ids": ["estateline:1"],
           "checked_at": "2026-10-10T16:00:00Z", "status": "unresolved",
           "sources": [{"url": f"https://example.org/evidence/{index}"}
                       for index in range(5)], "pages_consulted": 5}
    result.write_text(json.dumps(row) + "\n")
    output = tmp_path / "evidence"
    path = matching.write_search_evidence(review, [result], output, research)
    assert matching.write_search_evidence(review, [result], output, research) == path
    manifest = json.loads((path / "manifest.json").read_text())
    assert manifest["consulted_pages"] == 5
    assert manifest["searched_address_tasks"] == 1
    row["sources"].append({"url": "https://example.org/evidence/6"})
    row["pages_consulted"] = 6
    result.write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError, match="page cap"):
        matching.write_search_evidence(review, [result], output, research)


def test_search_evidence_accepts_single_target_research_shape(tmp_path) -> None:
    review = tmp_path / "review"
    review.mkdir()
    (review / "manifest.json").write_text(json.dumps({"run_id": "developer-review-test"}))
    task = {"address_key": "address_" + "b" * 24,
            "target_ids": ["osm_construction:way/123/v1"], "status": "unsearched"}
    (review / "search_tasks.jsonl").write_text(json.dumps(task) + "\n")
    research = tmp_path / "research"
    research.mkdir()
    observation = {"address_key": task["address_key"],
                   "target": {"target_id": task["target_ids"][0]},
                   "observed_at": "2026-10-10T16:00:00Z",
                   "assessment": {"link_status": "unresolved"},
                   "evidence": [{"source_url": "https://example.org/permit/123"}]}
    path = research / "observation.jsonl"
    path.write_text(json.dumps(observation) + "\n")
    output = matching.write_search_evidence(review, [path], tmp_path / "output", research)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["searched_address_tasks"] == 1
    assert manifest["consulted_pages"] == 1


def test_search_evidence_keeps_accepted_targets_and_separate_address_tasks(tmp_path) -> None:
    review = tmp_path / "review"
    review.mkdir()
    (review / "manifest.json").write_text(json.dumps({"run_id": "developer-review-test"}))
    (review / "search_tasks.jsonl").write_text("")  # Both project links were accepted.
    tasks = [{"address_key": "address_" + char * 24,
              "target_ids": [f"estateline:{index}"], "status": "unsearched"}
             for index, char in ((1, "a"), (2, "b"))]
    (review / "search_tasks_evidence_scope.jsonl").write_text(
        "".join(json.dumps(task) + "\n" for task in tasks))
    research = tmp_path / "research"
    research.mkdir()
    row = {"target_ids": ["estateline:1", "estateline:2"],
           "checked_at": "2026-10-10T16:00:00Z", "status": "resolved",
           "sources": [{"url": "https://example.org/project"}], "pages_consulted": 1}
    source = research / "shared.jsonl"
    source.write_text(json.dumps(row) + "\n")
    output = matching.write_search_evidence(review, [source], tmp_path / "output", research)
    manifest = json.loads((output / "manifest.json").read_text())
    assert (manifest["searched_address_tasks"], manifest["search_scope_address_tasks"],
            manifest["queued_address_tasks"], manifest["consulted_pages"]) == (2, 2, 0, 2)
    indexed = [json.loads(line) for line in (output / "index.jsonl").read_text().splitlines()]
    assert {row["address_key"] for row in indexed} == {task["address_key"] for task in tasks}


def test_search_evidence_counts_shared_pages_once_and_excludes_archived(tmp_path) -> None:
    review = tmp_path / "review"
    review.mkdir()
    (review / "manifest.json").write_text(json.dumps({"run_id": "developer-review-test"}))
    active = {"address_key": "address_" + "c" * 24,
              "target_ids": ["estateline:1", "estateline:2"], "status": "unsearched"}
    archived = {"address_key": "address_" + "d" * 24,
                "target_ids": ["estateline:3"], "status": "deferred_archived"}
    (review / "search_tasks.jsonl").write_text(
        "".join(json.dumps(task) + "\n" for task in (active, archived)))
    research = tmp_path / "research"
    research.mkdir()
    common = {"checked_at": "2026-10-10T16:00:00Z", "status": "unresolved",
              "sources": [{"url": "https://example.org/a"},
                          {"url": "https://example.org/b"}], "pages_consulted": 2}
    rows = [{**common, "target_ids": [target_id]}
            for target_id in (*active["target_ids"], *archived["target_ids"])]
    source = research / "shared.jsonl"
    source.write_text("".join(json.dumps(row) + "\n" for row in rows))
    output = matching.write_search_evidence(review, [source], tmp_path / "output", research)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["searched_address_tasks"] == 1
    assert manifest["consulted_pages"] == 2
    assert manifest["manual_target_assessments"] == 2


def test_search_evidence_can_cite_unopened_source_excerpt(tmp_path) -> None:
    review = tmp_path / "review"
    review.mkdir()
    (review / "manifest.json").write_text(json.dumps({"run_id": "developer-review-test"}))
    task = {"address_key": "address_" + "e" * 24,
            "target_ids": ["estateline:1"], "status": "unsearched"}
    (review / "search_tasks.jsonl").write_text(json.dumps(task) + "\n")
    research = tmp_path / "research"
    research.mkdir()
    source = research / "excerpt.jsonl"
    source.write_text(json.dumps({"target_ids": task["target_ids"],
                                  "checked_at": "2026-10-10T16:00:00Z",
                                  "sources": [{"url": "https://example.org/large.pdf"}],
                                  "pages_consulted": 0}) + "\n")
    output = matching.write_search_evidence(review, [source], tmp_path / "output", research)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["searched_address_tasks"] == 1
    assert manifest["consulted_pages"] == 0
