# -*- encoding: utf-8 -*-
import os

class PostcardBackup:

    @staticmethod
    def create_backup(source_dir, archive_path, compression_level=10):
        """
        Crée une archive tar compressée en zstd.
        """
        import tarfile
        import zstandard as zstd

        source_dir = os.path.abspath(source_dir)

        cctx = zstd.ZstdCompressor(level=compression_level)

        with open(archive_path, "wb") as archive_file:
            with cctx.stream_writer(archive_file) as compressor:
                with tarfile.open(fileobj=compressor, mode="w|") as tar:
                    tar.add(
                        os.path.join(source_dir, 'cards'),
                        arcname='cards'
                    )
                    for f in ['travels.json']:
                        fname = os.path.join(source_dir, f)
                        if os.path.isfile(fname):
                            tar.add(
                                fname,
                                arcname=f
                            )


    @staticmethod
    def extract_backup(archive_path, destination_dir):
        """
        Extrait une archive tar.zst.
        """
        import tarfile
        import zstandard as zstd

        os.makedirs(destination_dir, exist_ok=True)

        dctx = zstd.ZstdDecompressor()

        with open(archive_path, "rb") as archive_file:
            with dctx.stream_reader(archive_file) as reader:
                with tarfile.open(fileobj=reader, mode="r|") as tar:
                    tar.extractall(path=destination_dir)
