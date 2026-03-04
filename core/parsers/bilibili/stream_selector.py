class BiliStreamSelector:
    @staticmethod
    def _pick_stream_url(item: dict) -> str | None:
        url = item.get("baseUrl") or item.get("base_url")
        if url:
            return url
        backups = item.get("backupUrl") or item.get("backup_url") or []
        if backups and isinstance(backups, list):
            return backups[0]
        return None

    def select_best_stream_offline(self, data: dict, duration: int, limit_mb: int) -> tuple[str | None, str | None]:
        """
        返回 (video_url, audio_url)
        """
        if "dash" not in data:
            if "durl" in data and data["durl"]:
                return data["durl"][0].get("url"), None
            return None, None

        dash = data["dash"]
        video_streams = [v for v in dash.get("video", []) if v.get("id", 0) <= 64]
        audio_streams = dash.get("audio", [])

        if not video_streams:
            return None, None

        audio_size_mb = 0.0
        audio_url = None
        if audio_streams:
            best_audio = audio_streams[0]
            audio_url = self._pick_stream_url(best_audio)
            bandwidth = best_audio.get("bandwidth", 128000)
            audio_size_mb = (bandwidth / 8 * duration) / 1024 / 1024

        remaining_mb = max(limit_mb - audio_size_mb, 0)

        video_streams.sort(key=lambda x: x.get("id", 0), reverse=True)
        selected_v_url = None

        for v in video_streams:
            bandwidth = v.get("bandwidth", 0)
            est_size_mb = (bandwidth / 8 * duration) / 1024 / 1024
            # 1.25 预留波动空间
            if est_size_mb * 1.25 <= remaining_mb:
                selected_v_url = self._pick_stream_url(v)
                if selected_v_url:
                    break

        if selected_v_url is None and video_streams:
            selected_v_url = self._pick_stream_url(video_streams[-1])

        return selected_v_url, audio_url