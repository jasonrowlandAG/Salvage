# Salvage bench results

Generated 2026-09-06T13:37:00+00:00

| Engine | FS | Scenario | Recall | Precision | Name acc. | Path acc. | Date acc. | Junk | Time (s) | Notes |
|---|---|---|---|---|---|---|---|---|---|---|
| photorec | fat32 | delete_all | 85% | 100% | 0% | 0% | 0% | 0 | 0.1 |  |
| photorec | fat32 | delete_subset | 85% | 100% | 0% | 0% | 0% | 0 | 0.1 |  |
| photorec | fat32 | delete_folder_tree | 85% | 100% | 0% | 0% | 0% | 0 | 0.1 | rm -r Documents/Reports |
| photorec | fat32 | quick_format | 65% | 100% | 0% | 0% | 0% | 0 | 0.1 | filesystem metadata was replaced by the reformat; name/path accuracy here measures whether an engine can dig up remnants of the old metadata, not just carve data |
| photorec | fat32 | partial_overwrite | 85% | 100% | 0% | 0% | 0% | 0 | 0.0 | overwrote 25165824 bytes (25% of the volume) starting at offset 52419831; ground truth was computed by re-scanning the raw image for each file's exact bytes after the overwrite, not by guessing |
| photorec | fat32 | fragmentation | 85% | 85% | 0% | 0% | 0% | 3 | 0.1 | deliberately fragmented then deleted: ['Videos/Clips/clip_01.mp4', 'Videos/Clips/clip_02.mp4', 'DCIM/100APPLE/IMG_0006.JPG'] |
| photorec | fat32 | emptied_trash | 85% | 100% | 0% | 0% | 0% | 0 | 0.1 | moved to .Trashes/501 then deleted: ['DCIM/100APPLE/IMG_0002.JPG', 'Music/song_01.mp3', 'Documents/notes.txt'] |
| photorec | exfat | delete_all | 90% | 100% | 0% | 0% | 0% | 0 | 0.1 |  |
| photorec | exfat | delete_subset | 90% | 100% | 0% | 0% | 0% | 0 | 0.1 |  |
| photorec | exfat | delete_folder_tree | 90% | 100% | 0% | 0% | 0% | 0 | 0.1 | rm -r Documents/Reports |
| photorec | exfat | quick_format | 60% | 100% | 0% | 0% | 0% | 0 | 0.0 | filesystem metadata was replaced by the reformat; name/path accuracy here measures whether an engine can dig up remnants of the old metadata, not just carve data |
| photorec | exfat | partial_overwrite | 90% | 100% | 0% | 0% | 0% | 0 | 0.0 | overwrote 25165824 bytes (25% of the volume) starting at offset 52419831; ground truth was computed by re-scanning the raw image for each file's exact bytes after the overwrite, not by guessing |
| photorec | exfat | fragmentation | 90% | 86% | 0% | 0% | 0% | 3 | 0.1 | deliberately fragmented then deleted: ['Videos/Clips/clip_01.mp4', 'Videos/Clips/clip_02.mp4', 'DCIM/100APPLE/IMG_0006.JPG'] |
| photorec | exfat | emptied_trash | 90% | 100% | 0% | 0% | 0% | 0 | 0.1 | moved to .Trashes/501 then deleted: ['DCIM/100APPLE/IMG_0002.JPG', 'Music/song_01.mp3', 'Documents/notes.txt'] |
| photorec | hfs+ | delete_all | 85% | 94% | 0% | 0% | 0% | 1 | 0.1 |  |
| photorec | hfs+ | delete_subset | 85% | 94% | 0% | 0% | 0% | 1 | 0.1 |  |
| photorec | hfs+ | delete_folder_tree | 85% | 94% | 0% | 0% | 0% | 1 | 0.0 | rm -r Documents/Reports |
| photorec | hfs+ | quick_format | 80% | 94% | 0% | 0% | 0% | 1 | 0.0 | filesystem metadata was replaced by the reformat; name/path accuracy here measures whether an engine can dig up remnants of the old metadata, not just carve data |
| photorec | hfs+ | partial_overwrite | 89% | 94% | 0% | 0% | 0% | 1 | 0.0 | overwrote 25165824 bytes (25% of the volume) starting at offset 52419831; ground truth was computed by re-scanning the raw image for each file's exact bytes after the overwrite, not by guessing |
| photorec | hfs+ | fragmentation | 90% | 86% | 0% | 0% | 0% | 3 | 0.1 | deliberately fragmented then deleted: ['Videos/Clips/clip_01.mp4', 'Videos/Clips/clip_02.mp4', 'DCIM/100APPLE/IMG_0006.JPG'] |
| photorec | hfs+ | emptied_trash | 85% | 94% | 0% | 0% | 0% | 1 | 0.1 | moved to .Trashes/501 then deleted: ['DCIM/100APPLE/IMG_0002.JPG', 'Music/song_01.mp3', 'Documents/notes.txt'] |
| photorec | apfs | delete_all | 75% | 100% | 0% | 0% | 0% | 0 | 0.1 |  |
| photorec | apfs | delete_subset | 90% | 100% | 0% | 0% | 0% | 0 | 0.0 |  |
| photorec | apfs | delete_folder_tree | 90% | 100% | 0% | 0% | 0% | 0 | 0.0 | rm -r Documents/Reports |
| photorec | apfs | quick_format | 75% | 100% | 0% | 0% | 0% | 0 | 0.1 | filesystem metadata was replaced by the reformat; name/path accuracy here measures whether an engine can dig up remnants of the old metadata, not just carve data |
| photorec | apfs | partial_overwrite | 88% | 100% | 0% | 0% | 0% | 0 | 0.0 | overwrote 25165824 bytes (25% of the volume) starting at offset 52419831; ground truth was computed by re-scanning the raw image for each file's exact bytes after the overwrite, not by guessing |
| photorec | apfs | fragmentation | 90% | 95% | 0% | 0% | 0% | 1 | 0.1 | deliberately fragmented then deleted: ['Videos/Clips/clip_01.mp4', 'Videos/Clips/clip_02.mp4', 'DCIM/100APPLE/IMG_0006.JPG'] |
| photorec | apfs | emptied_trash | 90% | 100% | 0% | 0% | 0% | 0 | 0.0 | moved to .Trashes/501 then deleted: ['DCIM/100APPLE/IMG_0002.JPG', 'Music/song_01.mp3', 'Documents/notes.txt'] |
