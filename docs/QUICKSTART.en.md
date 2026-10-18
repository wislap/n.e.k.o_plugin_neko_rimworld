# Get started with N.E.K.O RimWorld

Ask about your colony in your N.E.K.O conversation, then read full answers and past discussions in the plugin panel. The plugin reads game information and offers advice; it does not issue game commands.

You need the RimWorld Bridge Mod, Harmony, the N.E.K.O RimWorld plugin, and an OpenAI-compatible model service. Follow your distribution's installation and host-version instructions. The current source is still being prepared for its first release; no new package has been published.

## Connect the game

1. Enable Harmony and N.E.K.O RimWorld Bridge in the game's Mod list and restart as prompted.
2. In Mod Settings, open **N.E.K.O RimWorld Bridge**. Choose **Generate new pairing code**, then select **Enable N.E.K.O connection**.
3. Copy the **Connection port** and **Pairing code**, then close the settings window to save and apply them.
4. Start the RimWorld plugin in N.E.K.O's plugin manager. Open its panel's **Settings**, enter the same port and pairing code, then select **Save and apply connection settings**.
5. Load your colony. Once the panel shows **Game connected**, you can open the connection check and read a brief colony overview. This does not call a model.

Leave the pairing code blank to retain it. If you generate a new code in the game, update the plugin too. The plugin does not auto-start by default, so check that it is running before use. Consultations work while the game is paused.

## Set up the model

Enter the service URL, model name, and any required API key under **Model service**, then choose **Save model settings**. You can usually keep the advanced limits at their defaults.

This is a separate model for RimWorld; it does not inherit your N.E.K.O character's model settings. Consultations send relevant game information to that service and may incur charges. Saving settings, viewing history, and checking the game connection do not call a model. **Configured** does not mean the model service has been tested.

Leave the API key blank to keep the saved key. Saved keys are not returned to the panel, and unsaved key drafts are not written to browser storage.

## Understand save status

- **Saving**: wait for this operation to finish; switching panel pages is fine. You do not need to submit again.
- **Saved and applied**: the write was acknowledged, or an authoritative configuration read confirmed it, and the plugin applied it.
- **Not applied**: review the information shown before trying again.
- **Unconfirmed**: the changes may already have been written. Check the same save's status first; after a plugin reload, review the current settings before deciding whether to save again. Fully closing or reloading the panel discards unsaved input.

An uncertain reply never causes an automatic second write or a success message. Closing or reloading the plugin can interrupt a save. Up to 20 receipts are retained during the current plugin process.

## Ask and read

Return to your N.E.K.O conversation and ask, “In RimWorld, what should I deal with first?”

**Latest advice** shows the most recent discussion for the current colony. Expand the full answer or copy it directly. Switching panel pages does not cancel a consultation; stopping or reloading the plugin does. Older advice remains historical and does not automatically track later game changes.

Use **History** to find questions or characters, or limit results to the current colony. Continue a discussion in the same character's N.E.K.O conversation by mentioning the question or people involved. Reading or refreshing history never starts another consultation.

**Troubleshooting** retains execution details, original readings, and a consultation test. The consultation test calls your model; the connection check does not. Browsing these pages does not run either operation automatically.

## If the answer is incomplete

Read the explanation shown with the answer. Incomplete analysis, uncertain saving, and unavailable file exports are identified separately. If no downloadable file is available, retained text can still be read or copied. You do not need another consultation just to keep the answer.

Open that answer's troubleshooting details for more information. Saved content reflects the game when it was read, and can be browsed offline. The panel provides English and Simplified Chinese. Chinese locale variants use Simplified Chinese; other unsupported languages use English.
