import sys
import re
import json
import random
import os
from typing import Tuple, Optional, List, Dict
import os.path
from beir import util
from beir.datasets.data_loader import GenericDataLoader
import pandas as pd
import tensorflow as tf
import tensorflow_ranking as tfr
import keras_tuner as kt

from vespa.package import (
    ApplicationPackage,
    Schema,
    Document,
    Field,
    FieldSet,
    RankProfile as Ranking,
    QueryProfile,
    QueryField,
)
from vespa.evaluation import NormalizedDiscountedCumulativeGain as NDCG
from vespa.query import QueryModel, RankProfile, WeakAnd

REPLACE_SYMBOLS = ["(", ")", " -", " +"]
QUOTES = [
    "\u0022",  # quotation mark (")
    "\u0027",  # apostrophe (')
    "\u00ab",  # left-pointing double-angle quotation mark
    "\u00bb",  # right-pointing double-angle quotation mark
    "\u2018",  # left single quotation mark
    "\u2019",  # right single quotation mark
    "\u201a",  # single low-9 quotation mark
    "\u201b",  # single high-reversed-9 quotation mark
    "\u201c",  # left double quotation mark
    "\u201d",  # right double quotation mark
    "\u201e",  # double low-9 quotation mark
    "\u201f",  # double high-reversed-9 quotation mark
    "\u2039",  # single left-pointing angle quotation mark
    "\u203a",  # single right-pointing angle quotation mark
    "\u300c",  # left corner bracket
    "\u300d",  # right corner bracket
    "\u300e",  # left white corner bracket
    "\u300f",  # right white corner bracket
    "\u301d",  # reversed double prime quotation mark
    "\u301e",  # double prime quotation mark
    "\u301f",  # low double prime quotation mark
    "\ufe41",  # presentation form for vertical left corner bracket
    "\ufe42",  # presentation form for vertical right corner bracket
    "\ufe43",  # presentation form for vertical left corner white bracket
    "\ufe44",  # presentation form for vertical right corner white bracket
    "\uff02",  # fullwidth quotation mark
    "\uff07",  # fullwidth apostrophe
    "\uff62",  # halfwidth left corner bracket
    "\uff63",  # halfwidth right corner bracket
]
REPLACE_SYMBOLS.extend(QUOTES)


class Dataset:
    def __init__(self):
        """
        Convenient funnctions to remove special symbols from queries.
        """
        pass

    @staticmethod
    def replace_symbols(x):
        for symbol in REPLACE_SYMBOLS:
            x = x.replace(symbol, "")
        return x

    @staticmethod
    def parse_query(query):
        return re.sub(" +", " ", Dataset.replace_symbols(query)).strip()


class BeirData:
    def __init__(self, data_dir: str, dataset_name: str):
        """
        Download, sample and standardized data format to be used in the ranking framework.

        :param data_dir: Root folder to store datasets
        :param dataset_name: Name of the dataset to manipulate.
        """
        self.data_dir = data_dir
        self.dataset_name = dataset_name
        self.dataset_dir = os.path.join(self.data_dir, self.dataset_name)

    def download_and_unzip_dataset(self) -> str:
        """
        Download and unzip dataset

        :return: Return the path of the folder containing the unzipped dataset files.
        """
        url = "https://public.ukp.informatik.tu-darmstadt.de/thakur/BEIR/datasets/{}.zip".format(
            self.dataset_name
        )
        data_path = util.download_and_unzip(url, self.data_dir)
        print("Dataset downloaded here: {}".format(data_path), file=sys.stdout)
        return data_path

    def prepare_data(self, split_type: str) -> Tuple:
        """
        Extract corpus, queries and qrels from the dataset.

        :param split_type: One of 'train', 'dev' or 'test' set.

        :return: a tuple containing 'corpus', 'queries' and 'qrels'.
        """
        corpus, queries, qrels = GenericDataLoader(self.dataset_dir).load(
            split=split_type
        )  # or split = "train" or "dev"
        return corpus, queries, qrels

    @staticmethod
    def sample_positive_data(qrels, queries, number_samples):
        """
        Sample qrels, queries and positive document ids.

        :param qrels: Dict containing query id as key and a dict with doc_id:score as value.
        :param queries: Dict containing query id as key and query string as value.
        :param number_samples: The number of positive (query_id, relevant_doc)-pairs to sample.

        :return: Tuple with the following elements: qrels_sample, queries_sample and positive_id_sample
        """

        qrels_sample = {
            k: qrels[k]
            for k in random.sample(k=number_samples, population=sorted(qrels))
        }
        queries_sample = {k: queries[k] for k in qrels_sample.keys()}
        positive_id_sample = [
            doc_id[0]
            for doc_id in [list(docs.keys()) for docs in qrels_sample.values()]
        ]
        return qrels_sample, queries_sample, positive_id_sample

    def load_full_data(self, split_types: Optional[List] = None):
        """
        Load full dataset, parse it and return it in a standardized format.

        :param split_types: A list containing a combination of 'train', 'dev' and 'test' set. Default to ['train', 'dev']
        :return: A dictionary containing 'corpus' and 'split' keys.
        """

        if not split_types:
            split_types = ["train", "dev"]
        assert len(split_types) > 0, "Specify at least one split_type."

        split = {}
        corpus = None
        for split_type in split_types:
            corpus, queries, qrels = self.prepare_data(split_type=split_type)
            split[split_type] = {"qrels": qrels, "queries": queries}

        return {"corpus": corpus, "split": split}

    def sample_data(
        self,
        number_positive_samples,
        number_negative_samples,
        split_types: Optional[List] = None,
        seed: Optional[int] = None,
    ):
        """
        Routine to sample smaller datasets for prototyping.

        :param number_positive_samples: The number of positive (query_id, relevant_doc)-pairs to select.
            If number_positive_samples=100 it means we will sample 100 pairs for each split type.
            The relevant documents will be included in the document corpus.
        :param number_negative_samples: The number of documents to be randomly chosen from the document corpus, in
            addition to the relevant documents sampled.
        :param split_types: A list containing a combination of 'train', 'dev' and 'test' set.
            Default to ['train', 'dev']
        :param seed: Seed to initialize the random number generator.

        :return: Dict containing the following keys: 'corpus', 'train_qrels', 'train_queries', 'dev_qrels', 'dev_queries'.
        """
        if seed:
            random.seed(seed)

        if not split_types:
            split_types = ["train", "dev"]

        assert len(split_types) > 0, "Specify at least one split_type."

        positive_ids = []
        split = {}
        corpus = None
        for split_type in split_types:
            corpus, queries, qrels = self.prepare_data(split_type=split_type)
            (
                qrels_sample,
                queries_sample,
                positive_id_sample,
            ) = self.sample_positive_data(
                qrels=qrels,
                queries=queries,
                number_samples=number_positive_samples,
            )
            positive_ids = positive_ids + positive_id_sample
            split[split_type] = {"qrels": qrels_sample, "queries": queries_sample}

        negative_ids = random.sample(
            k=number_negative_samples, population=sorted(corpus)
        )
        doc_id_samples = list(set(positive_ids + negative_ids))
        corpus_sample = {k: corpus[k] for k in doc_id_samples}

        return {"corpus": corpus_sample, "split": split}

    def save_sample(self, sample: Dict, file_name: str = "sample.json"):
        """
        Convenient function to save sample data

        :param sample: data generated by :func:`sample_data`.
        :param file_name: Name of the file to store the sample data
        """
        file_path = os.path.join(self.dataset_dir, file_name)
        with open(file_path, "w") as f:
            json.dump(sample, f)

    def load_sample(self, file_name: str = "sample.json"):
        """
        Load sample data

        :param file_name: Name of the sample file.
        :return: Sample data
        """
        file_path = os.path.join(self.dataset_dir, file_name)
        with open(file_path, "r") as f:
            sample = json.load(f)
        return sample


class SparseBeirApplicationPackage(ApplicationPackage):
    def __init__(self, name: str = "SparseBeir"):
        """
        Create an application package suited to perform sparse ranking on beir data.

        :param name: Name of the application package
        """
        document = Document(
            fields=[
                Field(name="id", type="string", indexing=["attribute", "summary"]),
                Field(
                    name="title",
                    type="string",
                    indexing=["index"],
                    index="enable-bm25",
                ),
                Field(
                    name="body",
                    type="string",
                    indexing=["index"],
                    index="enable-bm25",
                ),
            ]
        )
        schema = Schema(
            name=name,
            document=document,
            fieldsets=[FieldSet(name="default", fields=["title", "body"])],
            rank_profiles=[
                Ranking(
                    name="bm25",
                    first_phase="bm25(body)",
                    summary_features=["bm25(body)"],
                ),
                Ranking(name="native_rank", first_phase="nativeRank(body)"),
                Ranking(name="random", first_phase="random"),
            ],
        )
        super().__init__(
            name=name,
            schema=[schema],
            query_profile=QueryProfile(
                fields=[QueryField(name="maxHits", value=10000)]
            ),
        )

    def add_first_phase_linear_model(self, name, weights):
        """
        Add a linear model as a first phase ranking

        :param name: Name of the ranking profile.
        :param weights: Dict containing feature name as key and weight value as value.
        """
        self.schema.add_rank_profile(
            Ranking(
                name=name,
                first_phase=" + ".join(
                    ["{} * {}".format(k, v) for k, v in weights.items()]
                ),
            )
        )


class SparseBeirApp:
    def __init__(self, app):
        """
        Class containing convenient methods related to the beir data sparse app.
        """
        self.app = app

    def feed(self, data):
        """
        Feed beir data corpus to a Vespa app.

        :param data: Dict containing data generated by the :class:`BeirData` class.
        :return: List with feed response returned by pyvespa.
        """
        corpus = data["corpus"]
        batch_feed = [
            {
                "id": idx,
                "fields": {
                    "id": idx,
                    "title": corpus[idx].get("title", None),
                    "body": corpus[idx].get("text", None),
                },
            }
            for idx in list(corpus.keys())
        ]
        feed_results = self.app.feed_batch(batch=batch_feed)
        return feed_results

    @staticmethod
    def create_labeled_data_from_beir_data(qrels, queries):
        """
        Create pyvespa labeled data from beir datasets

        :param qrels: Dict containing query id as key and a dict with doc_id:score as value.
        :param queries: Dict containing query id as key and query string as value.
        :return: pyvespa labeled data
        """
        labeled_data = [
            {
                "query_id": int(query_id),
                "query": Dataset.parse_query(queries[query_id]),
                "relevant_docs": [
                    {"id": relevant_doc_id, "score": qrels[query_id][relevant_doc_id]}
                    for relevant_doc_id in qrels[query_id].keys()
                ],
            }
            for query_id in qrels.keys()
        ]
        return labeled_data

    def collect_vespa_features(
        self,
        data,
        split_type,
        number_additional_docs,
        batch_size=100,
    ):
        """
        Collect labeled data with Vespa rank features.

        :param data: Dict containing data generated by the :class:`BeirData` class.
        :param split_type: One of 'train', 'dev' and 'test' set.
        :param number_additional_docs: The number of additional documents to collect for each labeled data point.
        :param batch_size: Size of the batch to send for each request.

        :return: Data frame containing collected features
        """
        labeled_data = self.create_labeled_data_from_beir_data(
            qrels=data["split"][split_type]["qrels"],
            queries=data["split"][split_type]["queries"],
        )
        labeled_data_batches = [
            labeled_data[i : i + batch_size]
            for i in range(0, len(labeled_data), batch_size)
        ]
        query_model = QueryModel(
            match_phase=WeakAnd(hits=number_additional_docs),
            rank_profile=RankProfile(name="random", list_features=True),
        )
        training_data = []
        for idx, ld in enumerate(labeled_data_batches):
            training_data_batch = self.app.collect_training_data(
                labeled_data=ld,
                id_field="id",
                query_model=query_model,
                number_additional_docs=number_additional_docs,
                fields=["rankfeatures", "summaryfeatures"],
            )
            print("{}/{}".format(idx, len(labeled_data_batches)))
            training_data.append(training_data_batch)
        df = pd.concat(training_data, ignore_index=True)
        df = df.drop_duplicates(["document_id", "query_id", "label"])
        return df

    def evaluate(self, data, query_model, metric=NDCG(at=10), split_type="dev"):
        """
        Evaluate query model over a specific split type of the beir data. Default to NDCG @ 10 metric.

        :param data: Dict containing data generated by the :class:`BeirData` class.
        :param query_model: QueryModel that we want to evaluate.
        :param metric: Metric to use in the evaluation. Default to NDCG @ 10.
        :param split_type: Split type to use from the BeirData. Default to 'dev'.
        :return:
        """
        labeled_data = self.create_labeled_data_from_beir_data(
            qrels=data["split"][split_type]["qrels"],
            queries=data["split"][split_type]["queries"],
        )
        vespa_metric = self.app.evaluate(
            labeled_data=labeled_data,
            eval_metrics=[metric],
            query_model=query_model,
            id_field="id",
        )
        return vespa_metric.loc[metric.name].loc["mean", query_model.name]


def keras_linear_model(
    number_documents_per_query,
    number_features,
):
    """
    linear model with a lasso constrain on the kernel weights.

    :param number_documents_per_query: Number of documents per query to reshape the listwise prediction.
    :param number_features: Number of features used per document.

    :return: The uncompiled Keras model.
    """
    model = tf.keras.Sequential()
    model.add(
        tf.keras.layers.Input(shape=(number_documents_per_query, number_features))
    )
    model.add(
        tf.keras.layers.Dense(
            1,
            use_bias=False,
            activation=None,
        )
    )
    model.add(tf.keras.layers.Reshape((number_documents_per_query,)))
    return model


def keras_lasso_linear_model(
    number_documents_per_query,
    number_features,
    l1_penalty,
    normalization_layer=None,
):
    """
    linear model with a lasso constrain on the kernel weights.

    :param number_documents_per_query: Number of documents per query to reshape the listwise prediction.
    :param number_features: Number of features used per document.
    :param normalization_layer: Initialized normalization layers. Used when performing feature selection.

    :return: The uncompiled Keras model.
    """
    model = tf.keras.Sequential()
    model.add(
        tf.keras.layers.Input(shape=(number_documents_per_query, number_features))
    )
    if normalization_layer:
        model.add(normalization_layer)
    model.add(
        tf.keras.layers.Dense(
            1,
            use_bias=False,
            activation=None,
            kernel_regularizer=tf.keras.regularizers.L1(l1_penalty),
        )
    )
    model.add(tf.keras.layers.Reshape((number_documents_per_query,)))
    return model


def keras_ndcg_compiled_model(model, learning_rate, top_n):
    """
    Compile listwise Keras model with NDCG stateless metric and ApproxNDCGLoss

    :param model: uncompiled Keras model
    :param learning_rate: learning rate used in the Adagrad optim algo.
    :return: Keras compiled model.
    """
    ndcg = tfr.keras.metrics.NDCGMetric(topn=top_n)

    def ndcg_stateless(y_true, y_pred):
        ndcg.reset_states()
        return ndcg(y_true, y_pred)

    optimizer = tf.keras.optimizers.Adagrad(learning_rate)
    model.compile(
        optimizer=optimizer,
        loss=tfr.keras.losses.ApproxNDCGLoss(),
        metrics=ndcg_stateless,
    )
    return model


class LinearHyperModel(kt.HyperModel):
    def __init__(
        self,
        number_documents_per_query,
        number_features,
        top_n=10,
        learning_rate_range=None,
    ):
        self.number_documents_per_query = number_documents_per_query
        self.number_features = number_features
        self.top_n = top_n
        if not learning_rate_range:
            learning_rate_range = [1e-2, 1e2]
        self.learning_rate_range = learning_rate_range
        super().__init__()

    def build(self, hp):
        model = keras_linear_model(
            number_documents_per_query=self.number_documents_per_query,
            number_features=self.number_features,
        )
        compiled_model = keras_ndcg_compiled_model(
            model=model,
            learning_rate=hp.Float(
                "learning_rate",
                min_value=self.learning_rate_range[0],
                max_value=self.learning_rate_range[1],
                sampling="log",
            ),
            top_n=self.top_n,
        )
        return compiled_model


class LassoHyperModel(kt.HyperModel):
    def __init__(
        self,
        number_documents_per_query,
        number_features,
        trained_normalization_layer,
        top_n=10,
        l1_penalty_range=None,
        learning_rate_range=None,
    ):
        self.number_documents_per_query = number_documents_per_query
        self.number_features = number_features
        self.trained_normalization_layer = trained_normalization_layer
        self.top_n = top_n
        if not l1_penalty_range:
            l1_penalty_range = [1e-4, 1e-2]
        self.l1_penalty_range = l1_penalty_range
        if not learning_rate_range:
            learning_rate_range = [1e-2, 1e2]
        self.learning_rate_range = learning_rate_range
        super().__init__()

    def build(self, hp):
        model = keras_lasso_linear_model(
            number_documents_per_query=self.number_documents_per_query,
            number_features=self.number_features,
            l1_penalty=hp.Float(
                "lambda",
                min_value=self.l1_penalty_range[0],
                max_value=self.l1_penalty_range[1],
                sampling="log",
            ),
            normalization_layer=self.trained_normalization_layer,
        )
        compiled_model = keras_ndcg_compiled_model(
            model=model,
            learning_rate=hp.Float(
                "learning_rate",
                min_value=self.learning_rate_range[0],
                max_value=self.learning_rate_range[1],
                sampling="log",
            ),
            top_n=self.top_n,
        )
        return compiled_model


class ListwiseRankingFramework:
    def __init__(
        self,
        number_documents_per_query,
        batch_size,
        tuner_max_trials,
        tuner_executions_per_trial,
        tuner_epochs,
        tuner_early_stop_patience,
        final_epochs,
        top_n=10,
        l1_penalty_range=None,
        learning_rate_range=None,
        folder_dir=os.getcwd(),
        project_name="listwise_ranking_framework",
    ):
        self.number_documents_per_query = number_documents_per_query
        self.batch_size = batch_size
        self.tuner_max_trials = tuner_max_trials
        self.tuner_executions_per_trial = tuner_executions_per_trial
        self.tuner_epochs = tuner_epochs
        self.tuner_early_stop_patience = tuner_early_stop_patience
        self.final_epochs = final_epochs
        self.top_n = top_n
        self.l1_penalty_range = l1_penalty_range
        self.learning_rate_range = learning_rate_range
        self.folder_dir = folder_dir
        self.project_name = project_name

    def listwise_dataset_from_collected_features(
        self,
        df,
        feature_names,
    ):
        """
        Create TensorFlow dataframe suited for listwise loss function from pandas df.

        :param df: Pandas df containing the data.
        :param feature_names: Features to be used in the tensorflow model.
        :param number_documents_per_query: Number of documents per query. This will be used as the batch size
            of the TF dataset.
        :return: TF dataset
        """
        query_id_name = "query_id"
        target_name = "label"
        ds = tf.data.Dataset.from_tensor_slices(
            {
                "features": tf.cast(df[feature_names].values, tf.float32),
                "label": tf.cast(df[target_name].values, tf.float32),
                "query_id": tf.cast(df[query_id_name].values, tf.int64),
            }
        )

        key_func = lambda x: x[query_id_name]
        reduce_func = lambda key, dataset: dataset.batch(
            self.number_documents_per_query, drop_remainder=True
        )
        listwise_ds = ds.group_by_window(
            key_func=key_func,
            reduce_func=reduce_func,
            window_size=self.number_documents_per_query,
        )
        listwise_ds = listwise_ds.map(lambda x: (x["features"], x["label"]))
        return listwise_ds

    def create_and_train_normalization_layer(self, train_ds):
        normalization_layer = tf.keras.layers.Normalization()
        train_feature_ds = train_ds.map(lambda x, y: x)
        normalization_layer.adapt(train_feature_ds.batch(self.batch_size))
        return normalization_layer

    def tune_linear_model(
        self,
        train_df,
        dev_df,
        feature_names,
    ):

        number_features = len(feature_names)
        train_ds = self.listwise_dataset_from_collected_features(
            df=train_df,
            feature_names=feature_names,
        )
        dev_ds = self.listwise_dataset_from_collected_features(
            df=dev_df,
            feature_names=feature_names,
        )
        linear_hyper_model = LinearHyperModel(
            number_documents_per_query=self.number_documents_per_query,
            number_features=number_features,
            top_n=self.top_n,
            learning_rate_range=self.learning_rate_range,
        )
        tuner = kt.RandomSearch(
            linear_hyper_model,
            objective=kt.Objective("val_ndcg_stateless", direction="max"),
            directory=self.folder_dir,
            project_name=self.project_name,
            overwrite=True,
            max_trials=self.tuner_max_trials,
            executions_per_trial=self.tuner_executions_per_trial,
        )
        early_stopping_callback = tf.keras.callbacks.EarlyStopping(
            monitor="val_ndcg_stateless",
            patience=self.tuner_early_stop_patience,
            mode="max",
        )
        tuner.search(
            train_ds.batch(self.batch_size),
            validation_data=dev_ds.batch(self.batch_size),
            epochs=self.tuner_epochs,
            callbacks=[early_stopping_callback],
        )
        best_hyperparams = tuner.get_best_hyperparameters()[0].values
        print(best_hyperparams)
        best_hps = tuner.get_best_hyperparameters()[0]
        model = linear_hyper_model.build(best_hps)
        model.fit(
            train_ds.batch(self.batch_size),
            validation_data=dev_ds.batch(self.batch_size),
            epochs=self.final_epochs,
        )
        weights = model.get_weights()
        weights = {
            "feature_names": feature_names,
            "linear_model_weights": [
                float(weights[0][idx][0]) for idx in range(len(feature_names))
            ],
        }
        eval_result_from_fit = model.history.history["val_ndcg_stateless"][-1]

        return weights, eval_result_from_fit, best_hyperparams

    def tune_lasso_linear_model(
        self,
        train_df,
        dev_df,
        feature_names,
    ):

        number_features = len(feature_names)
        train_ds = self.listwise_dataset_from_collected_features(
            df=train_df,
            feature_names=feature_names,
        )
        dev_ds = self.listwise_dataset_from_collected_features(
            df=dev_df,
            feature_names=feature_names,
        )
        trained_normalization_layer = self.create_and_train_normalization_layer(
            train_ds=train_ds
        )
        lasso_hyper_model = LassoHyperModel(
            number_documents_per_query=self.number_documents_per_query,
            number_features=number_features,
            trained_normalization_layer=trained_normalization_layer,
            top_n=self.top_n,
            l1_penalty_range=self.l1_penalty_range,
            learning_rate_range=self.learning_rate_range,
        )
        tuner = kt.RandomSearch(
            lasso_hyper_model,
            objective=kt.Objective("val_ndcg_stateless", direction="max"),
            directory=self.folder_dir,
            project_name=self.project_name,
            overwrite=True,
            max_trials=self.tuner_max_trials,
            executions_per_trial=self.tuner_executions_per_trial,
        )
        early_stopping_callback = tf.keras.callbacks.EarlyStopping(
            monitor="val_ndcg_stateless",
            patience=self.tuner_early_stop_patience,
            mode="max",
        )
        tuner.search(
            train_ds.batch(self.batch_size),
            validation_data=dev_ds.batch(self.batch_size),
            epochs=self.tuner_epochs,
            callbacks=[early_stopping_callback],
        )
        best_hyperparams = tuner.get_best_hyperparameters()[0].values
        print(best_hyperparams)
        best_hps = tuner.get_best_hyperparameters()[0]
        model = lasso_hyper_model.build(best_hps)
        model.fit(
            train_ds.batch(self.batch_size),
            validation_data=dev_ds.batch(self.batch_size),
            epochs=self.final_epochs,
        )
        weights = model.get_weights()
        weights = {
            "feature_names": feature_names,
            "normalization_mean": weights[0].tolist(),
            "normalization_sd": weights[1].tolist(),
            "normalization_number_data": int(weights[2]),
            "linear_model_weights": [
                float(weights[3][idx][0]) for idx in range(len(feature_names))
            ],
        }
        eval_result_from_fit = model.history.history["val_ndcg_stateless"][-1]

        return weights, eval_result_from_fit, best_hyperparams

    def lasso_model_search(
        self,
        train_df,
        dev_df,
        feature_names,
        protected_features=None,
        output_file="lasso_model_search.json",
    ):

        output_file = os.path.join(self.folder_dir, self.project_name, output_file)
        try:
            with open(output_file, "r") as f:
                results = json.load(f)
                print("Lasso model search: Results from output file loaded.")
        except FileNotFoundError:
            print("Lasso model search: File not found. Starting search from scratch.")
            results = []

        if not protected_features:
            protected_features = []
        while (len(feature_names) >= len(protected_features)) and len(
            feature_names
        ) > 0:
            (weights, evaluation, best_hyperparams) = self.tune_lasso_linear_model(
                train_df=train_df,
                dev_df=dev_df,
                feature_names=feature_names,
            )
            partial_result = {
                "evaluation": evaluation,
                "weights": weights,
                "best_hyperparams": best_hyperparams,
            }
            results.append(partial_result)
            with open(output_file, "w") as f:
                json.dump(results, f)

            weights = {
                feature_name: float(model_weight)
                for feature_name, model_weight in zip(
                    weights["feature_names"], weights["linear_model_weights"]
                )
            }
            print({k: round(weights[k], 2) for k in weights})
            print(evaluation)

            abs_weights = {k: abs(weights[k]) for k in weights}
            if protected_features:
                abs_weights = {
                    k: abs_weights[k]
                    for k in abs_weights
                    if k not in protected_features
                }
            if len(abs_weights) > 0:
                worst_feature = min(abs_weights, key=abs_weights.get)
                feature_names = [x for x in feature_names if x != worst_feature]
            else:
                break

        return results

    def _forward_selection_iteration(self, train_df, dev_df, feature_names):
        (weights, evaluation, best_hyperparams) = self.tune_lasso_linear_model(
            train_df=train_df,
            dev_df=dev_df,
            feature_names=feature_names,
        )
        partial_result = {
            "number_features": len(feature_names),
            "evaluation": evaluation,
            "weights": weights,
            "best_hyperparams": best_hyperparams,
        }
        weights = {
            feature_name: float(model_weight)
            for feature_name, model_weight in zip(
                weights["feature_names"], weights["linear_model_weights"]
            )
        }
        print({k: round(weights[k], 2) for k in weights})
        print(evaluation)
        return partial_result

    def forward_selection_model_search(
        self,
        train_df,
        dev_df,
        feature_names,
        maximum_number_of_features=None,
        output_file="forward_selection_model_search.json",
        protected_features=None,
    ):

        output_file = os.path.join(self.folder_dir, self.project_name, output_file)
        try:
            with open(output_file, "r") as f:
                results = json.load(f)
                print(
                    "Forward selection model search: Results from output file loaded."
                )
        except FileNotFoundError:
            print(
                "Forward selection model search: File not found. Starting search from scratch."
            )
            results = []

        if not maximum_number_of_features:
            maximum_number_of_features = len(feature_names)
        maximum_number_of_features = min(maximum_number_of_features, len(feature_names))

        if not protected_features:
            protected_features = []
        else:
            partial_result = self._forward_selection_iteration(
                train_df=train_df, dev_df=dev_df, feature_names=protected_features
            )
            results.append(partial_result)
        while len(protected_features) < maximum_number_of_features:
            best_eval = 0
            best_features = None
            feature_names = [x for x in feature_names if x not in protected_features]
            for new_feature in feature_names:
                experimental_features = protected_features + [new_feature]
                partial_result = self._forward_selection_iteration(
                    train_df=train_df,
                    dev_df=dev_df,
                    feature_names=experimental_features,
                )
                evaluation = partial_result["evaluation"]
                results.append(partial_result)
                if evaluation > best_eval:
                    best_eval = evaluation
                    best_features = experimental_features
                with open(output_file, "w") as f:
                    json.dump(results, f)
            protected_features = best_features
        return results
