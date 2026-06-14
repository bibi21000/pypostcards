# -*- encoding: utf-8 -*-
from pathlib import Path
import pickle
import tempfile
import requests
import imagehash
import open_clip
import torch

from PIL import Image, ImageGrab


class PostcardSearcher:

    def __init__(
        self,
        # ~ model_name="ViT-B-32",
        # ~ pretrained="laion2b_s34b_b79k",
        model_name="ViT-L-14",
        pretrained="laion2b_s32b_b82k",
        tqdm=list,
    ):

        self.device = (
            "cuda"
            if torch.cuda.is_available()
            else "cpu"
        )

        self.model, _, self.preprocess = (
            open_clip.create_model_and_transforms(
                model_name,
                pretrained=pretrained
            )
        )

        self.model = self.model.to(self.device)
        self.model.eval()

        self.index = {}
        self.tqdm = tqdm

    # --------------------------------------------------

    def compute_phash(self, image_path):

        image = Image.open(image_path).convert("RGB")

        return imagehash.phash(image)

    # --------------------------------------------------

    def compute_hashes(self, image_path):

        image = Image.open(image_path).convert("RGB")

        tensor = (
            self.preprocess(image)
            .unsqueeze(0)
            .to(self.device)
        )

        with torch.no_grad():

            emb = self.model.encode_image(tensor)

            emb /= emb.norm(
                dim=-1,
                keepdim=True
            )

        embedding = emb.squeeze().cpu()

        return {
            "path" : str(image_path),
            "mpath" : image_path.stat().st_mtime,
            "ahash" : imagehash.average_hash(image),
            "dhash" : imagehash.dhash(image),
            "phash" : imagehash.phash(image),
            "whash" : imagehash.whash(image),
            "embedding" : embedding,
        }

    # --------------------------------------------------

    @staticmethod
    def hash_similarity(h1, h2):

        distance = h1 - h2

        return max(
            0.0,
            100 * (1 - distance / 64)
        )

    def multi_hash_similarity(
        self,
        hashes1,
        hashes2
    ):

        scores = {

            "ahash": self.hash_similarity(
                hashes1["ahash"],
                hashes2["ahash"]
            ),

            "phash": self.hash_similarity(
                hashes1["phash"],
                hashes2["phash"]
            ),

            "dhash": self.hash_similarity(
                hashes1["dhash"],
                hashes2["dhash"]
            ),

            "whash": self.hash_similarity(
                hashes1["whash"],
                hashes2["whash"]
            )
        }

        weights = {
            "ahash": 0.15,
            "phash": 0.40,
            "dhash": 0.20,
            "whash": 0.25
        }

        return sum(
            scores[k] * weights[k]
            for k in scores
        )

    def compute_embedding(self, image_path):

        image = Image.open(image_path).convert("RGB")

        tensor = (
            self.preprocess(image)
            .unsqueeze(0)
            .to(self.device)
        )

        with torch.no_grad():

            emb = self.model.encode_image(tensor)

            emb /= emb.norm(
                dim=-1,
                keepdim=True
            )

        return emb.squeeze().cpu()

    # --------------------------------------------------

    def build_index(self, location):

        # ~ self.index = []
        # ~ self.index = {}

        location = Path(location)

        if location.is_dir():
            files = []

            for ext in (".png", ".tif", ".tiff"):

                files.extend(
                    Path(location).rglob(f"*_R{ext}")
                )
        else:
            files = [location]

        for file in self.tqdm(files):

            sfile = str(file)
            if sfile in self.index and file.stat().st_mtime <= self.index[sfile]['mpath']:
                continue

            try:

                self.index[str(file)] = self.compute_hashes(file)
                # ~ self.index.append({

                    # ~ "path": str(file),

                    # ~ "phash":
                        # ~ self.compute_phash(file),

                    # ~ "embedding":
                        # ~ self.compute_embedding(file)

                # ~ })

            except Exception as exc:

                print(file, exc)

        return len(self.index)

    # --------------------------------------------------

    def save_index(self, filename):

        with open(filename, "wb") as f:

            pickle.dump(self.index, f)

    # --------------------------------------------------

    def load_index(self, filename):

        if isinstance(filename, str):
            filename = Path(filename)

        if filename.exists():
            with open(filename, "rb") as f:

                self.index = pickle.load(f)
        else:
            self.index = {}

    # --------------------------------------------------

    @staticmethod
    def phash_similarity(h1, h2):

        distance = h1 - h2

        return max(
            0,
            100 * (1 - distance / 64)
        )

    @staticmethod
    def whash_similarity(h1, h2):

        distance = h1 - h2

        return max(
            0,
            100 * (1 - distance / 64)
        )

    # --------------------------------------------------

    @staticmethod
    def embedding_similarity(e1, e2):

        score = torch.dot(e1, e2).item()

        return max(0, score * 100)

    # --------------------------------------------------

    def search_file(
        self,
        image_path,
        threshold=70,
        max_results=20,
        hash_weight=0.60,
        clip_weight=0.40
    ):

        # ~ q_hash = self.compute_phash(image_path)

        # ~ q_emb = self.compute_embedding(image_path)

        if isinstance(image_path, str):
            image_path = Path(image_path)

        hashes = self.compute_hashes(image_path)

        results = []

        for item in self.tqdm(self.index.values()):

            # ~ phash_score = self.phash_similarity(
                # ~ q_hash,
                # ~ item["phash"]
            # ~ )

            # ~ clip_score = self.embedding_similarity(
                # ~ q_emb,
                # ~ item["embedding"]
            # ~ )

            # ~ final_score = (
                # ~ phash_weight * phash_score
                # ~ +
                # ~ clip_weight * clip_score
            # ~ )
            hash_score = self.multi_hash_similarity(
                hashes,
                item
            )

            clip_score = self.embedding_similarity(
                hashes["embedding"],
                item["embedding"]
            )

            final_score = (
                hash_weight * hash_score
                +
                clip_weight * clip_score
            )

            if final_score >= threshold:

                results.append({

                    "score": round(
                        final_score,
                        2
                    ),

                    "path":
                        item["path"]

                })

        results.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        return results[:max_results]

    # --------------------------------------------------

    def search_directory(
        self,
        query_dir,
        threshold=70,
        max_results=20
    ):

        extensions = {
            ".jpg",
            ".jpeg",
            ".png",
            ".bmp",
            ".webp",
            ".tif",
            ".tiff"
        }

        output = {}

        for file in Path(query_dir).rglob("*"):

            if file.suffix.lower() not in extensions:
                continue

            output[str(file)] = self.search_file(
                file,
                threshold,
                max_results
            )

        return output

    def search_url(
        self,
        image_url,
        threshold=70,
        max_results=20,
        hash_weight=0.60,
        clip_weight=0.40
    ):

        response = requests.get(
            image_url,
            timeout=30
        )

        response.raise_for_status()

        with tempfile.NamedTemporaryFile(
            suffix=".jpg",
            delete=True
        ) as tmp:

            tmp.write(response.content)
            tmp.flush()

            return self.search_file(
                tmp.name,
                threshold=threshold,
                max_results=max_results,
                hash_weight=hash_weight,
                clip_weight=clip_weight
            )

    def search_clipboard(
        self,
        threshold=70,
        max_results=20,
        hash_weight=0.60,
        clip_weight=0.40
    ):

        clipboard = ImageGrab.grabclipboard()

        if clipboard is None:
            raise ValueError(
                "Aucune image trouvée dans le presse-papiers."
            )

        if not hasattr(clipboard, "save"):
            raise ValueError(
                "Le presse-papiers ne contient pas une image."
            )

        with tempfile.NamedTemporaryFile(
            suffix=".png",
            delete=True
        ) as tmp:

            clipboard.save(tmp.name)

            return self.search_file(
                tmp.name,
                threshold=threshold,
                max_results=max_results,
                hash_weight=hash_weight,
                clip_weight=clip_weight
            )

    def find_similar_in_index(
        self,
        threshold=90,
        hash_weight=0.60,
        clip_weight=0.40
    ):

        matches = []

        sindex = [v for k,v in self.index.items()]

        total = len(sindex)

        for i in self.tqdm(range(total)):

            item1 = sindex[i]

            for j in range(i + 1, total):

                item2 = sindex[j]

                phash_score = self.multi_hash_similarity(
                    item1,
                    item2
                )

                clip_score = self.embedding_similarity(
                    item1["embedding"],
                    item2["embedding"]
                )

                final_score = (
                    hash_weight * phash_score +
                    clip_weight * clip_score
                )

                if final_score >= threshold:

                    matches.append({
                        "score": round(final_score, 2),
                        "file1": item1["path"],
                        "file2": item2["path"]
                    })

        matches.sort(
            key=lambda x: x["score"],
            reverse=True
        )

        return matches

    def extract_card_id(self, filepath: str) -> str:
        """
        Extrait l'id depuis :
        /.../396_R.tiff -> "396"
        """
        return Path(filepath).stem.split("_")[0]

    def verify_doubles(self, results, model):
        """
        Vérifie que :
          - id(file1) est dans doubles de file2
          - id(file2) est dans doubles de file1

        Retourne une liste enrichie avec le résultat de la vérification.
        """

        checked = []

        for item in results:
            id1 = self.extract_card_id(item["file1"])
            id2 = self.extract_card_id(item["file2"])

            card1 = model.get_card(id1)
            card2 = model.get_card(id2)

            doubles1 = set(str(x) for x in (card1.get("doubles", []) if card1 else []))
            doubles2 = set(str(x) for x in (card2.get("doubles", []) if card2 else []))

            id1_in_card2 = id1 in doubles2
            id2_in_card1 = id2 in doubles1

            checked.append({
                **item,
                "id1": id1,
                "id2": id2,
                "id1_in_file2_doubles": id1_in_card2,
                "id2_in_file1_doubles": id2_in_card1,
                "is_mutual_double": id1_in_card2 and id2_in_card1,
            })

        return checked


    def find_missing_doubles(
        self,
        model,
        results=None,
        threshold=90,
        hash_weight=0.60,
        clip_weight=0.40
    ):

        errors = []

        if results is None:
            results = self.find_similar_in_index(
                threshold=threshold,
                hash_weight=hash_weight,
                clip_weight=clip_weight
            )

        for item in results:
            id1 = self.extract_card_id(item["file1"])
            id2 = self.extract_card_id(item["file2"])

            card1 = model.get_card(id1)
            card2 = model.get_card(id2)

            doubles1 = set(str(x) for x in (card1.get("doubles", []) if card1 else []))
            doubles2 = set(str(x) for x in (card2.get("doubles", []) if card2 else []))

            if id1 not in doubles2 or id2 not in doubles1:
                errors.append({
                    "score": item["score"],
                    "file1": item["file1"],
                    "file2": item["file2"],
                    "id1": id1,
                    "id2": id2,
                    "file1_has_id2": id2 in doubles1,
                    "file2_has_id1": id1 in doubles2,
                })

        return errors

