import { useEffect, useRef } from "react";
import { Button } from "antd";
import { Mic, Square, Volume2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { isSttAvailable, useVoiceInput } from "../../hooks/useVoiceInput";
import { useVoiceOutput } from "../../hooks/useVoiceOutput";
import { useOptionalVoiceOutputContext } from "../../context/VoiceOutputContext";

export function TaskVoiceInput({
  disabled,
  onText,
}: {
  disabled: boolean;
  onText: (text: string) => void;
}) {
  const { t } = useTranslation();
  const { recording, transcribing, toggle } = useVoiceInput(onText);
  return (
    <Button
      icon={recording ? <Square size={15} /> : <Mic size={15} />}
      danger={recording}
      loading={transcribing}
      disabled={(disabled && !recording) || !isSttAvailable()}
      onClick={toggle}
    >
      {t(
        recording
          ? "projects.tasks.workbench.stopVoice"
          : "projects.tasks.workbench.voiceInput",
      )}
    </Button>
  );
}
export function TaskVoiceRead({ id, text }: { id: string; text: string }) {
  const { t } = useTranslation();
  const shared = useOptionalVoiceOutputContext();
  const local = useVoiceOutput();
  const voice = shared ?? local;
  const current = useRef(voice);
  current.current = voice;
  useEffect(
    () => () => {
      if (current.current.speakingId === id) current.current.stop();
    },
    [id],
  );
  return (
    <Button
      size="small"
      icon={
        voice.speakingId === id ? <Square size={14} /> : <Volume2 size={14} />
      }
      onClick={() => voice.speak(id, text)}
      disabled={!text.trim()}
    >
      {t(
        voice.speakingId === id
          ? "projects.tasks.workbench.stopReading"
          : "projects.tasks.workbench.readAloud",
      )}
    </Button>
  );
}
