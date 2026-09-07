# Salvage bench results

Generated 2026-09-07T02:58:21+00:00

Integrity columns score `salvage.engine.integrity.verify()` against ground truth: **Int. precision** is the fraction of verifier-INTACT files that are real (byte-exact) recoveries; **false-INTACT** is the dangerous error (verifier said INTACT, bytes are wrong); **false-CORRUPT** is a real recovery the verifier told the user to distrust.

| Engine | FS | Scenario | Recall | Precision | Name acc. | Path acc. | Date acc. | Junk | Int. precision | False-INTACT | False-CORRUPT | Time (s) | Notes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| photorec | fat32 | delete_all | 85% | 100% | 0% | 0% | 0% | 0 | 100% | 0% | 0% | 0.1 |  |
| filesystem | fat32 | delete_all | 100% | 100% | 30% | 100% | 55% | 0 | 100% | 0% | 0% | 0.3 |  |
| combined | fat32 | delete_all | 100% | 100% | 30% | 100% | 55% | 0 | 100% | 0% | 0% | 0.3 |  |
| photorec | fat32 | delete_subset | 85% | 100% | 0% | 0% | 0% | 0 | 100% | 0% | 0% | 0.0 |  |
| filesystem | fat32 | delete_subset | 50% | 100% | 30% | 100% | 60% | 0 | 100% | 0% | 0% | 0.2 |  |
| combined | fat32 | delete_subset | 95% | 100% | 16% | 53% | 32% | 0 | 100% | 0% | 0% | 0.2 |  |
| photorec | fat32 | delete_folder_tree | 85% | 100% | 0% | 0% | 0% | 0 | 100% | 0% | 0% | 0.1 | rm -r Documents/Reports |
| filesystem | fat32 | delete_folder_tree | 15% | 100% | 33% | 100% | 67% | 0 | 100% | 0% | 0% | 0.1 | rm -r Documents/Reports |
| combined | fat32 | delete_folder_tree | 90% | 100% | 6% | 17% | 11% | 0 | 100% | 0% | 0% | 0.1 | rm -r Documents/Reports |
| photorec | fat32 | quick_format | 65% | 100% | 0% | 0% | 0% | 0 | 100% | 0% | 0% | 0.0 | filesystem metadata was replaced by the reformat; name/path accuracy here measures whether an engine can dig up remnants of the old metadata, not just carve data |
| filesystem | fat32 | quick_format | 60% | 100% | 100% | 0% | 58% | 0 | 100% | 0% | 0% | 0.2 | filesystem metadata was replaced by the reformat; name/path accuracy here measures whether an engine can dig up remnants of the old metadata, not just carve data |
| combined | fat32 | quick_format | 75% | 100% | 80% | 0% | 47% | 0 | 100% | 0% | 0% | 0.2 | filesystem metadata was replaced by the reformat; name/path accuracy here measures whether an engine can dig up remnants of the old metadata, not just carve data |
| photorec | fat32 | partial_overwrite | 85% | 100% | 0% | 0% | 0% | 0 | 100% | 0% | 0% | 0.0 | overwrote 25165824 bytes (25% of the volume) starting at offset 52419831; ground truth was computed by re-scanning the raw image for each file's exact bytes after the overwrite, not by guessing |
| filesystem | fat32 | partial_overwrite | 100% | 100% | 30% | 100% | 55% | 0 | 100% | 0% | 0% | 0.3 | overwrote 25165824 bytes (25% of the volume) starting at offset 52419831; ground truth was computed by re-scanning the raw image for each file's exact bytes after the overwrite, not by guessing |
| combined | fat32 | partial_overwrite | 100% | 100% | 30% | 100% | 55% | 0 | 100% | 0% | 0% | 0.3 | overwrote 25165824 bytes (25% of the volume) starting at offset 52419831; ground truth was computed by re-scanning the raw image for each file's exact bytes after the overwrite, not by guessing |
| photorec | fat32 | fragmentation | 85% | 85% | 0% | 0% | 0% | 3 | 100% | 0% | 0% | 0.1 | deliberately fragmented then deleted: ['Videos/Clips/clip_01.mp4', 'Videos/Clips/clip_02.mp4', 'DCIM/100APPLE/IMG_0006.JPG'] |
| filesystem | fat32 | fragmentation | 15% | 100% | 0% | 100% | 33% | 0 | 100% | 0% | 0% | 0.1 | deliberately fragmented then deleted: ['Videos/Clips/clip_01.mp4', 'Videos/Clips/clip_02.mp4', 'DCIM/100APPLE/IMG_0006.JPG'] |
| combined | fat32 | fragmentation | 85% | 100% | 0% | 18% | 6% | 0 | 100% | 0% | 0% | 0.1 | deliberately fragmented then deleted: ['Videos/Clips/clip_01.mp4', 'Videos/Clips/clip_02.mp4', 'DCIM/100APPLE/IMG_0006.JPG'] |
| photorec | fat32 | emptied_trash | 85% | 100% | 0% | 0% | 0% | 0 | 100% | 0% | 0% | 0.1 | moved to .Trashes/501 then deleted: ['DCIM/100APPLE/IMG_0002.JPG', 'Music/song_01.mp3', 'Documents/notes.txt'] |
| filesystem | fat32 | emptied_trash | 15% | 100% | 0% | 100% | 0% | 0 | 100% | 0% | 0% | 0.1 | moved to .Trashes/501 then deleted: ['DCIM/100APPLE/IMG_0002.JPG', 'Music/song_01.mp3', 'Documents/notes.txt'] |
| combined | fat32 | emptied_trash | 90% | 100% | 0% | 17% | 0% | 0 | 100% | 0% | 0% | 0.2 | moved to .Trashes/501 then deleted: ['DCIM/100APPLE/IMG_0002.JPG', 'Music/song_01.mp3', 'Documents/notes.txt'] |
| photorec | exfat | delete_all | 90% | 100% | 0% | 0% | 0% | 0 | 100% | 0% | 0% | 0.1 |  |
| filesystem | exfat | delete_all | 100% | 100% | 100% | 100% | 0% | 0 | 100% | 0% | 0% | 0.3 |  |
| combined | exfat | delete_all | 100% | 100% | 100% | 100% | 0% | 0 | 100% | 0% | 0% | 0.3 |  |
| photorec | exfat | delete_subset | 90% | 100% | 0% | 0% | 0% | 0 | 100% | 0% | 0% | 0.1 |  |
| filesystem | exfat | delete_subset | 50% | 100% | 100% | 100% | 0% | 0 | 100% | 0% | 0% | 0.2 |  |
| combined | exfat | delete_subset | 95% | 100% | 53% | 53% | 0% | 0 | 100% | 0% | 0% | 0.2 |  |
| photorec | exfat | delete_folder_tree | 90% | 100% | 0% | 0% | 0% | 0 | 100% | 0% | 0% | 0.1 | rm -r Documents/Reports |
| filesystem | exfat | delete_folder_tree | 15% | 100% | 100% | 100% | 0% | 0 | 100% | 0% | 0% | 0.1 | rm -r Documents/Reports |
| combined | exfat | delete_folder_tree | 95% | 100% | 16% | 16% | 0% | 0 | 100% | 0% | 0% | 0.1 | rm -r Documents/Reports |
| photorec | exfat | quick_format | 60% | 100% | 0% | 0% | 0% | 0 | 100% | 0% | 0% | 0.1 | filesystem metadata was replaced by the reformat; name/path accuracy here measures whether an engine can dig up remnants of the old metadata, not just carve data |
| filesystem | exfat | quick_format | 50% | 100% | 100% | 0% | 0% | 0 | 100% | 0% | 0% | 0.2 | filesystem metadata was replaced by the reformat; name/path accuracy here measures whether an engine can dig up remnants of the old metadata, not just carve data |
| combined | exfat | quick_format | 65% | 100% | 77% | 0% | 0% | 0 | 100% | 0% | 0% | 0.2 | filesystem metadata was replaced by the reformat; name/path accuracy here measures whether an engine can dig up remnants of the old metadata, not just carve data |
| photorec | exfat | partial_overwrite | 90% | 100% | 0% | 0% | 0% | 0 | 100% | 0% | 0% | 0.0 | overwrote 25165824 bytes (25% of the volume) starting at offset 52419831; ground truth was computed by re-scanning the raw image for each file's exact bytes after the overwrite, not by guessing |
| filesystem | exfat | partial_overwrite | 100% | 100% | 100% | 100% | 0% | 0 | 100% | 0% | 0% | 0.3 | overwrote 25165824 bytes (25% of the volume) starting at offset 52419831; ground truth was computed by re-scanning the raw image for each file's exact bytes after the overwrite, not by guessing |
| combined | exfat | partial_overwrite | 100% | 100% | 100% | 100% | 0% | 0 | 100% | 0% | 0% | 0.3 | overwrote 25165824 bytes (25% of the volume) starting at offset 52419831; ground truth was computed by re-scanning the raw image for each file's exact bytes after the overwrite, not by guessing |
| photorec | exfat | fragmentation | 90% | 86% | 0% | 0% | 0% | 3 | 100% | 0% | 0% | 0.1 | deliberately fragmented then deleted: ['Videos/Clips/clip_01.mp4', 'Videos/Clips/clip_02.mp4', 'DCIM/100APPLE/IMG_0006.JPG'] |
| filesystem | exfat | fragmentation | 15% | 100% | 100% | 100% | 0% | 0 | 100% | 0% | 0% | 0.1 | deliberately fragmented then deleted: ['Videos/Clips/clip_01.mp4', 'Videos/Clips/clip_02.mp4', 'DCIM/100APPLE/IMG_0006.JPG'] |
| combined | exfat | fragmentation | 90% | 100% | 17% | 17% | 0% | 0 | 100% | 0% | 0% | 0.1 | deliberately fragmented then deleted: ['Videos/Clips/clip_01.mp4', 'Videos/Clips/clip_02.mp4', 'DCIM/100APPLE/IMG_0006.JPG'] |
| photorec | exfat | emptied_trash | 90% | 100% | 0% | 0% | 0% | 0 | 100% | 0% | 0% | 0.1 | moved to .Trashes/501 then deleted: ['DCIM/100APPLE/IMG_0002.JPG', 'Music/song_01.mp3', 'Documents/notes.txt'] |
| filesystem | exfat | emptied_trash | 15% | 100% | 100% | 100% | 0% | 0 | 100% | 0% | 0% | 0.1 | moved to .Trashes/501 then deleted: ['DCIM/100APPLE/IMG_0002.JPG', 'Music/song_01.mp3', 'Documents/notes.txt'] |
| combined | exfat | emptied_trash | 95% | 100% | 16% | 16% | 0% | 0 | 100% | 0% | 0% | 0.1 | moved to .Trashes/501 then deleted: ['DCIM/100APPLE/IMG_0002.JPG', 'Music/song_01.mp3', 'Documents/notes.txt'] |
