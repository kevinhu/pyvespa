import unittest
import os
import shutil

from vespa.package import (
    Document,
    Field,
    Schema,
    FieldSet,
    QueryTypeField,
    Function,
    SecondPhaseRanking,
    OnnxModel,
    RankProfile,
    ApplicationPackage,
    VespaCloud,
)


class TestCloudDeployment(unittest.TestCase):
    def setUp(self) -> None:
        #
        # Create application package
        #
        document = Document(
            fields=[
                Field(name="id", type="string", indexing=["attribute", "summary"]),
                Field(
                    name="title",
                    type="string",
                    indexing=["index", "summary"],
                    index="enable-bm25",
                ),
                Field(
                    name="body",
                    type="string",
                    indexing=["index", "summary"],
                    index="enable-bm25",
                ),
            ]
        )
        msmarco_schema = Schema(
            name="msmarco",
            document=document,
            fieldsets=[FieldSet(name="default", fields=["title", "body"])],
            rank_profiles=[
                RankProfile(name="default", first_phase="nativeRank(title, body)")
            ],
        )
        app_package = ApplicationPackage(name="msmarco", schema=msmarco_schema)
        #
        # Deploy on Vespa Cloud
        #
        self.vespa_cloud = VespaCloud(
            tenant="vespa-team",
            application="pyvespa-integration",
            key_content=os.getenv("VESPA_CLOUD_USER_KEY").replace(r"\n", "\n"),
            application_package=app_package,
        )
        self.disk_folder = os.path.join(os.getenv("WORK_DIR"), "sample_application")
        self.instance_name = "test"
        self.app = self.vespa_cloud.deploy(
            instance=self.instance_name, disk_folder=self.disk_folder
        )

    def test_data_operation(self):
        #
        # Make sure we start with no data with id = 1
        #
        _ = self.app.delete_data(schema="msmarco", data_id="1")
        #
        # Get data that does not exist
        #
        self.assertEqual(
            self.app.get_data(schema="msmarco", data_id="1").status_code, 404
        )
        #
        # Feed a data point
        #
        response = self.app.feed_data_point(
            schema="msmarco",
            data_id="1",
            fields={
                "id": "1",
                "title": "this is my first title",
                "body": "this is my first body",
            },
        )
        self.assertEqual(response.json()["id"], "id:msmarco:msmarco::1")
        #
        # Get data that exist
        #
        response = self.app.get_data(schema="msmarco", data_id="1")
        self.assertEqual(response.status_code, 200)
        self.assertDictEqual(
            response.json(),
            {
                "fields": {
                    "id": "1",
                    "title": "this is my first title",
                    "body": "this is my first body",
                },
                "id": "id:msmarco:msmarco::1",
                "pathId": "/document/v1/msmarco/msmarco/docid/1",
            },
        )
        #
        # Update data
        #
        response = self.app.update_data(
            schema="msmarco", data_id="1", fields={"title": "this is my updated title"}
        )
        self.assertEqual(response.json()["id"], "id:msmarco:msmarco::1")
        #
        # Get the updated data point
        #
        response = self.app.get_data(schema="msmarco", data_id="1")
        self.assertEqual(response.status_code, 200)
        self.assertDictEqual(
            response.json(),
            {
                "fields": {
                    "id": "1",
                    "title": "this is my updated title",
                    "body": "this is my first body",
                },
                "id": "id:msmarco:msmarco::1",
                "pathId": "/document/v1/msmarco/msmarco/docid/1",
            },
        )
        #
        # Delete a data point
        #
        response = self.app.delete_data(schema="msmarco", data_id="1")
        self.assertEqual(response.json()["id"], "id:msmarco:msmarco::1")
        #
        # Deleted data should be gone
        #
        self.assertEqual(
            self.app.get_data(schema="msmarco", data_id="1").status_code, 404
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.disk_folder, ignore_errors=True)


class TestOnnxModelCloudDeployment(unittest.TestCase):
    def setUp(self) -> None:
        #
        # Create application package
        #
        self.app_package = ApplicationPackage(name="cord19")
        self.app_package.schema.add_fields(
            Field(name="cord_uid", type="string", indexing=["attribute", "summary"]),
            Field(
                name="title",
                type="string",
                indexing=["index", "summary"],
                index="enable-bm25",
            ),
            Field(
                name="doc_token_ids",
                type="tensor<float>(d0[96])",
                indexing=["attribute", "summary"],
            ),
        )
        self.app_package.schema.add_field_set(
            FieldSet(name="default", fields=["title"])
        )
        self.app_package.query_profile_type.add_fields(
            QueryTypeField(
                name="ranking.features.query(query_token_ids)",
                type="tensor<float>(d0[32])",
            )
        )
        self.app_package.schema.add_model(
            OnnxModel(
                model_name="bert_tiny",
                model_file_path=os.path.join(
                    os.getenv("RESOURCES_DIR"), "bert_tiny.onnx"
                ),
                inputs={
                    "input_ids": "input_ids",
                    "token_type_ids": "token_type_ids",
                    "attention_mask": "attention_mask",
                },
                outputs={"logits": "logits"},
            )
        )
        self.app_package.schema.add_rank_profile(
            RankProfile(
                name="bert",
                inherits="default",
                constants={"TOKEN_NONE": 0, "TOKEN_CLS": 101, "TOKEN_SEP": 102},
                functions=[
                    Function(
                        name="question_length",
                        expression="sum(map(query(query_token_ids), f(a)(a > 0)))",
                    ),
                    Function(
                        name="doc_length",
                        expression="sum(map(attribute(doc_token_ids), f(a)(a > 0)))",
                    ),
                    Function(
                        name="input_ids",
                        expression="tensor<float>(d0[1],d1[128])(\n"
                        "    if (d1 == 0,\n"
                        "        TOKEN_CLS,\n"
                        "    if (d1 < question_length + 1,\n"
                        "        query(query_token_ids){d0:(d1-1)},\n"
                        "    if (d1 == question_length + 1,\n"
                        "        TOKEN_SEP,\n"
                        "    if (d1 < question_length + doc_length + 2,\n"
                        "        attribute(doc_token_ids){d0:(d1-question_length-2)},\n"
                        "    if (d1 == question_length + doc_length + 2,\n"
                        "        TOKEN_SEP,\n"
                        "        TOKEN_NONE\n"
                        "    ))))))",
                    ),
                    Function(
                        name="attention_mask",
                        expression="map(input_ids, f(a)(a > 0)) ",
                    ),
                    Function(
                        name="token_type_ids",
                        expression="tensor<float>(d0[1],d1[128])(\n"
                        "    if (d1 < question_length,\n"
                        "        0,\n"
                        "    if (d1 < question_length + doc_length,\n"
                        "        1,\n"
                        "        TOKEN_NONE\n"
                        "    )))",
                    ),
                ],
                first_phase="bm25(title)",
                second_phase=SecondPhaseRanking(
                    rerank_count=10, expression="sum(onnx(bert_tiny).logits{d0:0,d1:0})"
                ),
                summary_features=[
                    "onnx(bert_tiny).logits",
                    "input_ids",
                    "attention_mask",
                    "token_type_ids",
                ],
            )
        )
        #
        # Deploy on Vespa Cloud
        #
        self.vespa_cloud = VespaCloud(
            tenant="vespa-team",
            application="pyvespa-integration",
            key_content=os.getenv("VESPA_CLOUD_USER_KEY").replace(r"\n", "\n"),
            application_package=self.app_package,
        )
        self.disk_folder = os.path.join(os.getenv("WORK_DIR"), "sample_application")
        self.instance_name = "test"

    def test_deployment(self):

        self.app = self.vespa_cloud.deploy(
            instance=self.instance_name, disk_folder=self.disk_folder
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.disk_folder, ignore_errors=True)