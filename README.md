# mktcmenu

A minimal, headless menu descriptor code generator for TCMenu.

## Improvements over the official TCMenu Designer (at least to me)

- Text-based descriptor format optimized specifically for readability and writing experience.
- Only generates the menu descriptor entries aka. things that are actually painful to write and maintain by hand and nothing platform/peripheral-specific unless absolutely necessary.
  - This means no initialization code is generated and you have to write those by hand. (It's usually just one-off and not that hard! Check TCMenu's documentation.)
  - In exchange you got better support for "unsupported hardware" that works perfectly in reality.
- Simple and fully automagic EEPROM management (just write `persistent: true`) with append-only allocation strategy for out-of-the-box backwards compatibility.
- Runs headlessly (although the official designer has this pending too).
- Hopefully better integration with PlatformIO.
- No more asking for registration upon every start (evil laugh).

## TODO

- More menu item types (i.e. most of the MultiPart entries).
- Better error handling.
- YAML schema for VSCode, etc.
- EEPROM mapping consistency check (overlaps, etc.).
