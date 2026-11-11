;; djlights — Beat Link Trigger expressions.
;; Loaded by the BLT Global Setup Expression via (load-file "./blt/vp_blt_code.clj")
;; (install.sh writes the absolute path into djlights.blt for you).
;;
;; Network interface the CDJs are on (e.g. "en5" for a USB-ethernet adapter).
;; Empty string = let BLT pick the interface itself (fine in most setups).
;; install.sh fills this in; you can also edit it by hand.
(def vp-cdj-iface "__CDJ_IFACE__")

;; Ports the djlights app listens on — must match the SETTINGS tab (Network).
(def vp-osc-port 9000)
(def vp-udp-port 9001)


(java.util.Locale/setDefault java.util.Locale/US)

;; Forward-declare functions used in futures before they are defined below.
(declare vp-osc!)

;; Dedicated agent for async state-file writes — keeps beat callback non-blocking.
(def ^:private vp-state-agent (agent nil :error-handler (fn [a e] nil)))


;; Start finders and overlay in background — must NOT block BLT's event thread.
;; Delay 1s to ensure VirtualCdj is running before we start dependent finders.
(future
  (Thread/sleep 1000)
  (doseq [[label ^Runnable start-fn]
          [["MetadataFinder"   #(.start (org.deepsymmetry.beatlink.data.MetadataFinder/getInstance))]
           ["WaveformFinder"   #(.start (org.deepsymmetry.beatlink.data.WaveformFinder/getInstance))]
           ["TimeFinder"       #(.start (org.deepsymmetry.beatlink.data.TimeFinder/getInstance))]
           ["AnalysisTagFinder" #(.start (org.deepsymmetry.beatlink.data.AnalysisTagFinder/getInstance))]
           ["SignatureFinder"  #(.start (org.deepsymmetry.beatlink.data.SignatureFinder/getInstance))]]]
    (try (start-fn) (println "[VP]" label "started")
         (catch Exception e (println "[VP]" label "start err:" (.getMessage e))))))

;; Auto-start BLT overlay web server — delayed so BLT's Swing UI is ready.
(future
  (Thread/sleep 2500)
  (try
    (if-let [run-fn (ns-resolve 'beat-link-trigger.overlay 'run-server)]
      (do (run-fn) (println "[VP] BLT overlay server started (run-server)"))
      (if-let [start-fn (ns-resolve 'beat-link-trigger.overlay 'start-server)]
        (do (start-fn 17081) (println "[VP] BLT overlay server started (start-server 17081)"))
        (println "[VP] Overlay: no suitable start function found")))
    (catch Exception e
      (println "[VP] Overlay server err:" (.getMessage e)))))

;; Auto-start nREPL server on port 17001 so VaultFX watchdog can nudge frozen triggers.
(future
  (Thread/sleep 4000)
  (try
    (require '[nrepl.server])
    (let [srv (nrepl.server/start-server :bind "127.0.0.1" :port 17001)]
      (swap! globals assoc :vp-nrepl {:server srv})
      (println "[VP] nREPL server started on port 17001"))
    (catch java.net.BindException _
      (println "[VP] nREPL port 17001 already in use — skipping"))
    (catch Exception e
      (println "[VP] nREPL start error:" (.getMessage e)))))

;; Register DeviceAnnouncementListener — fires when a CDJ goes offline.
;; This clears stale state and sends /blt/unload so Python removes the ghost panel.
(future
  (Thread/sleep 1200)
  (try
    (let [df (org.deepsymmetry.beatlink.DeviceFinder/getInstance)]
      (.addDeviceAnnouncementListener df
        (reify org.deepsymmetry.beatlink.DeviceAnnouncementListener
          (deviceFound [this ann] nil)
          (deviceLost  [this ann]
            (try
              (let [dev (int (.getDeviceNumber ann))]
                (when (<= dev 4)
                  (println "[VP] Device" dev "lost — clearing state + /blt/unload")
                  (swap! globals assoc-in [:vp-players dev :track-id]  -1)
                  (swap! globals assoc-in [:vp-players dev :is-playing] false)
                  (swap! globals assoc-in [:vp-players dev :last-pk]   "")
                  (swap! globals assoc-in [:vp-players dev :last-pd]   "")
                  (vp-osc! "/blt/unload" (int dev))))
              (catch Exception e (println "[VP] deviceLost err:" (.getMessage e))))))))
    (catch Exception e (println "[VP] DeviceAnnouncement listener err:" (.getMessage e)))))

;; Register AnalysisTagListener once finders are started (in background).
;; AnalysisTagUpdate has PUBLIC FIELDS: player (int), taggedSection (TaggedSection)
(future
  (Thread/sleep 1500)
  (try
    (let [atf (org.deepsymmetry.beatlink.data.AnalysisTagFinder/getInstance)]
      (when atf
        (let [listener
              (reify org.deepsymmetry.beatlink.data.AnalysisTagListener
                (analysisChanged [this event]
                  (try
                    (when event
                      (let [dev (try (int (.-player event)) (catch Exception _ -1))
                            ts  (try (.-taggedSection event) (catch Exception _ nil))]
                        (println "[VP] ATF FIRED dev=" dev " ts-type=" (class ts) " ts-nil=" (nil? ts))
                        (when (pos? dev)
                          (when ts
                            (swap! globals assoc-in [:vp-analysis dev] ts)
                            (swap! globals assoc :vp-map-keys-inspected false))
                          ;; Always force re-dump — even if ts is nil, ATF cache is now populated
                          (swap! globals assoc-in [:vp-players dev :last-dump] 0))))
                    (catch Exception e2
                      (println "[VP] ATF listener err:" (.getMessage e2))))))]
          (.addAnalysisTagListener atf listener ".EXT" "PSSI")
          (.addAnalysisTagListener atf listener ".PXZ" "PSSI")
          (println "[VP] ATF listeners registered (.EXT + .PXZ)"))))
    (catch Exception e
      (println "[VP] ATF listener setup err:" (.getMessage e)))))

;; Note: CrateDigger is managed by BLT 8.x core — no manual start needed.
;; ATF uses HTTP (port 12523) for CDJ-3000 phrase/analysis fetching automatically.

;; Enable colour waveform fetching
(try
  (let [wff (org.deepsymmetry.beatlink.data.WaveformFinder/getInstance)]
    (.setFindDetails wff true)
    (.setColorPreferred wff true))
  (catch Exception e
    (println "[VP] WaveformFinder init err:" (.getMessage e))))

;; OSC client → Python on localhost:9000
(try
  (swap! globals assoc :vp-osc (overtone.osc/osc-client "127.0.0.1" vp-osc-port))
  (catch Exception e
    (println "[VP] OSC client err:" (.getMessage e))))

;; UDP socket for track-dump JSON → Python on localhost:9001
(try
  (let [sock (java.net.DatagramSocket.)]
    (swap! globals assoc :vp-udp-sock sock)
    (swap! globals assoc :vp-udp-addr
           (java.net.InetSocketAddress. "127.0.0.1" (int vp-udp-port))))
  (catch Exception e
    (println "[VP] UDP socket err:" (.getMessage e))))

;; Per-player last-known state — all cleared on load so stale panels don't persist
(swap! globals assoc :vp-players
       {1 {:track-id -1 :phrase-idx -1 :phrases-json "[]" :is-playing false :last-pk "" :last-pd ""}
        2 {:track-id -1 :phrase-idx -1 :phrases-json "[]" :is-playing false :last-pk "" :last-pd ""}
        3 {:track-id -1 :phrase-idx -1 :phrases-json "[]" :is-playing false :last-pk "" :last-pd ""}
        4 {:track-id -1 :phrase-idx -1 :phrases-json "[]" :is-playing false :last-pk "" :last-pd ""}})

(println "[VP] Code file loaded OK")

;; ── OSC / UDP helpers ─────────────────────────────────────────────────────────

(defn vp-osc! [path & args]
  (try
    (when-let [c (:vp-osc @globals)]
      (apply overtone.osc/osc-send c path args))
    (catch Exception e
      (println "[VP] OSC send err" path (.getMessage e)))))

(defn vp-udp! [^String json]
  (try
    (let [sock (:vp-udp-sock @globals)
          addr (:vp-udp-addr @globals)
          b    (.getBytes json "UTF-8")]
      (when (and sock addr)
        (.send sock (java.net.DatagramPacket. b (count b) addr))))
    (catch Exception e
      (println "[VP] UDP send err:" (.getMessage e)))))

(defn vp-esc [^String s]
  (if (nil? s) ""
    (-> s
        (.replace "\\" "\\\\")
        (.replace "\"" "\\\"")
        (.replace "\n" "\\n")
        (.replace "\r" ""))))

;; Beat-link metadata fields like getArtist/getAlbum/getGenre return SearchableItem objects.
;; Extract the .label field; fall back to .toString if needed.
(defn vp-si->str [x]
  (try
    (cond
      (nil? x)             ""
      (instance? String x) x
      :else (or (try (.-label x) (catch Exception _ nil)) (str x)))
    (catch Exception _ "")))

;; Build DataReference from CdjStatus — needed for AnalysisTagFinder lookups
(defn vp-data-ref [cdj]
  (when cdj
    (try
      (org.deepsymmetry.beatlink.data.DataReference.
        (int (.getDeviceNumber cdj))
        (.getTrackSourceSlot cdj)
        (int (.getRekordboxId cdj)))
      (catch Exception e
        (println "[VP] vp-data-ref err:" (.getMessage e))
        nil))))

;; ── 3-band energy at playhead ─────────────────────────────────────────────────

(defn vp-energy [dev-num]
  (try
    (let [wff    (org.deepsymmetry.beatlink.data.WaveformFinder/getInstance)
          detail (.getLatestDetailFor wff (int dev-num))]
      (if detail
        (let [tf    (org.deepsymmetry.beatlink.data.TimeFinder/getInstance)
              upd   (.getLatestUpdateFor tf (int dev-num))
              pos   (when upd (.milliseconds upd))
              frames (.getFrameCount detail)
              total  (.getTotalTime detail)
              frame  (when (and pos total (> total 0) (> frames 0))
                       (long (Math/min (double (dec frames))
                                       (* (/ (double pos) total) frames))))]
          (if frame
            (let [f (int frame)]
              (try
                (let [col (.segmentColor detail f 31)
                      lo  (/ (.getBlue  col) 255.0)
                      mi  (/ (.getGreen col) 255.0)
                      hi  (/ (.getRed   col) 255.0)
                      amp (/ (.segmentHeight detail f 31) 31.0)]
                  [lo mi hi amp])
                (catch Exception _
                  (let [amp (/ (.segmentHeight detail f 31) 31.0)]
                    [amp amp amp amp]))))
            [0.0 0.0 0.0 0.0]))
        [0.0 0.0 0.0 0.0]))
    (catch Exception _ [0.0 0.0 0.0 0.0])))

;; ── Phrase helpers ────────────────────────────────────────────────────────────

(defn vp-phrase-kind [entry mood]
  (try
    (let [raw (try (.name (.id (.kind entry))) (catch Exception _ nil))]
      (if (nil? raw) ["unknown" "Unknown"]
        (cond
          (= mood 1)
          (cond
            (= raw "INTRO")  ["intro" "Intro"]
            (= raw "OUTRO")  ["outro" "Outro"]
            (= raw "CHORUS") (let [k1 (try (.k1 entry) (catch Exception _ 1))]
                               (if (= k1 1) ["chorus_1" "Chorus 1"] ["chorus_2" "Chorus 2"]))
            (= raw "UP")     (let [k3 (try (.k3 entry) (catch Exception _ 0))
                                   k2 (try (.k2 entry) (catch Exception _ 0))]
                               (cond (not= k3 0) ["up_3" "Up 3"]
                                     (not= k2 0) ["up_2" "Up 2"]
                                     :else        ["up_1" "Up 1"]))
            (= raw "DOWN")   (let [k3 (try (.k3 entry) (catch Exception _ 0))
                                   k2 (try (.k2 entry) (catch Exception _ 0))]
                               (cond (not= k3 0) ["down_3" "Down 3"]
                                     (not= k2 0) ["down_2" "Down 2"]
                                     :else        ["down_1" "Down 1"]))
            :else [(clojure.string/lower-case raw) raw])
          (= mood 2)
          (cond
            (= raw "INTRO")   ["intro"   "Intro"]
            (= raw "OUTRO")   ["outro"   "Outro"]
            (= raw "BRIDGE")  ["bridge"  "Bridge"]
            (= raw "CHORUS")  ["chorus"  "Chorus"]
            (= raw "VERSE_1") ["verse_1" "Verse 1"]
            (= raw "VERSE_2") ["verse_2" "Verse 2"]
            (= raw "VERSE_3") ["verse_3" "Verse 3"]
            (= raw "VERSE_4") ["verse_4" "Verse 4"]
            (= raw "VERSE_5") ["verse_5" "Verse 5"]
            (= raw "VERSE_6") ["verse_6" "Verse 6"]
            :else [(clojure.string/lower-case raw) raw])
          (= mood 3)
          (cond
            (= raw "INTRO")    ["intro"    "Intro"]
            (= raw "OUTRO")    ["outro"    "Outro"]
            (= raw "BRIDGE")   ["bridge"   "Bridge"]
            (= raw "CHORUS")   ["chorus"   "Chorus"]
            (= raw "VERSE_1")  ["verse_1"  "Verse 1"]
            (= raw "VERSE_1B") ["verse_1b" "Verse 1b"]
            (= raw "VERSE_2")  ["verse_2"  "Verse 2"]
            (= raw "VERSE_2B") ["verse_2b" "Verse 2b"]
            (= raw "VERSE_3")  ["verse_3"  "Verse 3"]
            :else [(clojure.string/lower-case raw) raw])
          :else ["unknown" "Unknown"])))
    (catch Exception _ ["unknown" "Unknown"])))

(defn vp-tag-from-atf [atf dev-num]
  "Get RekordboxAnlz$TaggedSection from ATF loaded-tags cache for dev-num.

   getLoadedAnalysisTags() returns Map<DeckReference, Map<String, CacheEntry>>
     - Outer key: DeckReference with public field 'player' (int)
     - Outer value: Map<String, CacheEntry> keyed by typeTag (e.g. 'PSSI')
     - CacheEntry has public field 'taggedSection' (RekordboxAnlz$TaggedSection)

   All field accesses use (.-field obj) because they are public Java fields, not methods."
  (when atf
    (try
      (let [loaded (.getLoadedAnalysisTags atf)]
        (when (and loaded (not (pos? (.size loaded))))
          (when (not (get-in @globals [:vp-atf-empty-logged dev-num]))
            (swap! globals assoc-in [:vp-atf-empty-logged dev-num] true)
            (println "[VP] ATF cache EMPTY dev=" dev-num " — ATF not fetching yet")))
        (when (and loaded (pos? (.size loaded)))
          ;; One-time diagnostic: show the nested structure
          (when (not (get-in @globals [:vp-atf-keys-logged dev-num]))
            (swap! globals assoc-in [:vp-atf-keys-logged dev-num] true)
            (println "[VP] ATF cache size=" (.size loaded) " dev=" dev-num)
            (doseq [outer-entry (.entrySet loaded)]
              (let [dk  (.getKey outer-entry)
                    imap (.getValue outer-entry)]
                (println "[VP]  DeckRef player=" (try (.-player dk) (catch Exception _ "?"))
                         " hotCue=" (try (.-hotCue dk) (catch Exception _ "?"))
                         " inner-type=" (class imap))
                (when (instance? java.util.Map imap)
                  (doseq [inner-entry (.entrySet imap)]
                    (println "[VP]    tag-key=" (.getKey inner-entry)
                             " entry-type=" (class (.getValue inner-entry))))))))
          ;; Find inner map matching our player, hotCue=0 (track-level, not hot-cue)
          (let [inner-map
                (some (fn [outer-entry]
                        (let [dk (.getKey outer-entry)]
                          (when (try (and (= (int (.-player dk)) (int dev-num))
                                         (= (int (.-hotCue dk)) 0))
                                     (catch Exception _ false))
                            (.getValue outer-entry))))
                      (.entrySet loaded))]
            (when (and inner-map (instance? java.util.Map inner-map))
              ;; Get the PSSI CacheEntry (phrase/song-structure analysis)
              (let [entry (or (.get inner-map "PSSI")
                              (first (vals inner-map)))]
                (when entry
                  ;; CacheEntry.taggedSection is a public field — use (.-field obj)
                  ;; In BLT 8.x entry may BE the tagged section / Map directly
                  (let [ts (try (.-taggedSection entry) (catch Exception _ nil))
                        result (or ts entry)]
                    (println "[VP] TaggedSection dev=" dev-num
                             " entry-type=" (class entry)
                             " ts-type=" (class ts)
                             " using-entry-directly=" (nil? ts))
                    result)))))))
      (catch Exception e
        (println "[VP] tag-from-atf err:" (.getMessage e))
        nil))))

(defn vp-map-get
  "Try getting a key from a Java Map — tries string and keyword variants."
  [m k]
  (when (instance? java.util.Map m)
    (or (.get m (name k))
        (.get m k)
        (.get m (clojure.string/replace (name k) "-" "_")))))

(defn vp-analysis-phrases [analysis]
  "Extract [phrase-vec mood-str bank-str].
   Handles both BLT 7.x RekordboxAnlz$TaggedSection (Java object with .body)
   and BLT 8.x Map representation."
  (when analysis
    (try
      (let [is-map (instance? java.util.Map analysis)

            ;; One-shot: dump Map keys to debug file so we can see the structure
            _ (when (and is-map
                         (not (get-in @globals [:vp-map-keys-inspected])))
                (swap! globals assoc :vp-map-keys-inspected true)
                (try
                  (spit "/tmp/vp_analysis_debug.txt"
                        (str "analysis type=" (class analysis)
                             "\nkeys=" (vec (.keySet analysis))
                             "\nvals=" (mapv class (.values analysis))
                             "\nraw=" (str analysis) "\n"))
                  (catch Exception _)))

            ;; Navigate to entries + mood + bank
            [entries mood-raw bank-raw]
            (if is-map
              ;; BLT 8.x Map — try several likely structures
              (let [;; Maybe map IS the body, or has a "body" key
                    body (or (vp-map-get analysis "body") analysis)
                    body2 (if (instance? java.util.Map body)
                            (or (vp-map-get body "body") body)
                            body)
                    ents (when (instance? java.util.Map body2)
                           (or (vp-map-get body2 "entries")
                               (vp-map-get body2 "phrases")
                               ;; maybe entries are the map values themselves
                               (let [vs (seq (.values body2))]
                                 (when (and vs (map? (first vs))) vs))))
                    m    (str (or (vp-map-get body2 "mood") ""))
                    b    (str (or (vp-map-get body2 "bank") ""))]
                [ents m b])
              ;; BLT 7.x TaggedSection objects
              (let [b1      (.body analysis)
                    body    (.body b1)
                    entries (try (seq (.entries body)) (catch Exception _ nil))
                    m       (try (str (.name (.mood body))) (catch Exception _ ""))
                    b       (try (str (.name (.bank body))) (catch Exception _ ""))]
                [entries m b]))

            mood (cond (.contains mood-raw "HIGH") "high"
                       (.contains mood-raw "MID")  "mid"
                       (.contains mood-raw "LOW")  "low"
                       :else "unknown")
            bank (cond (.contains bank-raw "COOL") "cool"
                       (.contains bank-raw "HOT")  "hot"
                       :else "natural")]
        (println "[VP] vp-analysis-phrases: entries=" (count entries)
                 " mood=" mood-raw " bank=" bank-raw " is-map=" is-map)
        [entries mood bank])
      (catch Exception e
        (println "[VP] analysis-phrases err:" (.getMessage e) " type=" (class analysis))
        nil))))


(defn vp-phrases [dev-num status]
  (try
    (let [atf      (org.deepsymmetry.beatlink.data.AnalysisTagFinder/getInstance)
          analysis (or (get-in @globals [:vp-analysis dev-num])
                       (vp-tag-from-atf atf dev-num))
          _        (when (and (nil? analysis) (not (get-in @globals [:vp-atf-debug dev-num])))
                     (swap! globals assoc-in [:vp-atf-debug dev-num] true)
                     (println "[VP] dev" dev-num "no analysis in cache"))
          [ph mood bank] (when analysis (vp-analysis-phrases analysis))]
      (if (seq ph)
        (let [pv (vec ph)
              n  (count pv)
              sb (java.lang.StringBuilder. "[")
              ;; Detect format: BLT 8.x map-entries vs Java TaggedSection entries
              is-map-entry? (map? (first pv))
              mood-int      (when-not is-map-entry?
                              (case mood "high" 1 "mid" 2 "low" 3 0))]
          (doseq [i (range n)]
            (let [e  (nth pv i)
                  bs (if is-map-entry? (:beat e 0)   (try (int (.beat e)) (catch Exception _ 0)))
                  bl (when is-map-entry? (:length e 0))
                  be (if is-map-entry?
                       (if (pos? bl) (+ bs bl) 0)
                       (if (< i (dec n)) (try (int (.beat (nth pv (inc i)))) (catch Exception _ 0)) 0))
                  hf (if is-map-entry? (boolean (:fill e)) (try (.fill e) (catch Exception _ false)))
                  fb (if is-map-entry? (:fill-beat e 0) (try (int (.beatFill e)) (catch Exception _ 0)))
                  nm (when is-map-entry? (:name e ""))
                  [k d] (if is-map-entry?
                          (let [k (-> nm clojure.string/lower-case (.replace " " "_"))] [k nm])
                          (vp-phrase-kind e mood-int))]
              (when (> i 0) (.append sb ","))
              (.append sb (str "{\"index\":" (inc i) ",\"beat_start\":" bs ",\"beat_end\":" be
                               ",\"kind\":\"" (vp-esc k) "\",\"display\":\"" (vp-esc d) "\""
                               ",\"fill\":" (if hf "true" "false") ",\"fill_beat\":" fb "}"))))
          (.append sb "]")
          [(str sb) (or mood "unknown") (or bank "natural")])
        ["[]" (or mood "unknown") (or bank "natural")]))
    (catch Exception e
      (println "[VP] phrases err:" (.getMessage e))
      ["[]" "unknown" "natural"])))

(defn vp-cues [meta]
  (try
    (if meta
      (let [cl  (.getCueList meta)
            ent (when cl (seq (.entries cl)))]
        (if ent
          (let [sb (java.lang.StringBuilder. "[")]
            (doseq [[i e] (map-indexed vector ent)]
              (let [hn  (try (int (.hotCueNumber e)) (catch Exception _ 0))
                    lbl (vp-esc (if (and (> hn 0) (<= hn 8))
                                  (str (char (+ (int \A) (dec hn)))) ""))
                    nm  (vp-esc (try (.getComment e) (catch Exception _ "")))
                    tp  (try (let [t (str (.getType e))]
                               (cond (.contains t "HOT_CUE") "hot_cue"
                                     (.contains t "LOOP")    "loop"
                                     (.contains t "MEMORY")  "memory"
                                     :else                   "cue"))
                             (catch Exception _ "cue"))
                    tm  (try (.-cueTime e) (catch Exception _ 0))
                    lp  (try (.-loopTime e) (catch Exception _ 0))
                    bt  (try (.-beatNumber e) (catch Exception _ 0))
                    col (try (let [c (.getColor e)]
                               (str (.getRed c) "," (.getGreen c) "," (.getBlue c)))
                             (catch Exception _ "0,0,0"))]
                (when (> i 0) (.append sb ","))
                (.append sb (str "{\"index\":" i ",\"label\":\"" lbl "\",\"name\":\"" nm "\""
                                 ",\"type\":\"" tp "\",\"time_ms\":" tm ",\"loop_end_ms\":" (or lp 0)
                                 ",\"beat\":" bt ",\"color\":[" col "]}"))))
            (.append sb "]") (str sb))
          "[]"))
      "[]")
    (catch Exception e (println "[VP] cues err:" (.getMessage e)) "[]")))

(defn vp-current-phrase [dev-num cur-beat status]
  (try
    (let [atf      (org.deepsymmetry.beatlink.data.AnalysisTagFinder/getInstance)
          analysis (or (get-in @globals [:vp-analysis dev-num])
                       (vp-tag-from-atf atf dev-num))
          [ph mood] (when analysis (vp-analysis-phrases analysis))]
      (when (seq ph)
        (let [pv            (vec ph)
              is-map-entry? (map? (first pv))
              mood-int      (when-not is-map-entry? (case mood "high" 1 "mid" 2 "low" 3 0))
              n             (count pv)
              beat-of       (fn [e] (if is-map-entry? (:beat e 0) (try (int (.beat e)) (catch Exception _ 0))))
              idx (loop [i (dec n)]
                    (cond (< i 0) 0
                          (<= (beat-of (nth pv i)) cur-beat) i
                          :else (recur (dec i))))
              e   (nth pv idx)
              bs  (beat-of e)
              bl  (when is-map-entry? (:length e 0))
              be  (if is-map-entry?
                    (if (pos? bl) (+ bs bl) 0)
                    (if (< idx (dec n)) (beat-of (nth pv (inc idx))) 0))
              hf  (if is-map-entry? (boolean (:fill e)) (try (.fill e) (catch Exception _ false)))
              fb  (if is-map-entry? (:fill-beat e 0) (try (int (.beatFill e)) (catch Exception _ 0)))
              nm  (when is-map-entry? (:name e ""))
              [k d] (if is-map-entry?
                      (let [k (-> nm clojure.string/lower-case (.replace " " "_"))] [k nm])
                      (vp-phrase-kind e mood-int))
              bip (- cur-beat bs)
              sec (cond (and hf (>= cur-beat fb))                "fill"
                        (< bip 4)                                 "start"
                        (and (pos? be) (>= cur-beat (- be 4)))    "end"
                        :else                                     "loop")]
          {:idx (inc idx) :kind k :display d
           :beat-start bs :beat-end be
           :beat-in-phrase bip :section sec})))
    (catch Exception _ nil)))

;; SignatureFinder in this beat-link version does NOT have getLatestSignatureFor/findPhraseAtTime.
;; Phrase detection uses ATF (AnalysisTagFinder) exclusively.

;; ── Track metadata → UDP port 9001 ───────────────────────────────────────────

(defn vp-dump-meta! [dev-num beat]
  (try
    ;; Prefer CdjStatus over Beat for analysis lookups — Beat lacks trackSourceSlot
    (let [vcj  (org.deepsymmetry.beatlink.VirtualCdj/getInstance)
          cdj  (when vcj
                 (let [u (try (.getLatestStatusFor vcj (int dev-num)) (catch Exception _ nil))]
                   (when (instance? org.deepsymmetry.beatlink.CdjStatus u) u)))
          status-for-analysis (or cdj beat)
          mdf  (org.deepsymmetry.beatlink.data.MetadataFinder/getInstance)
          meta (when mdf (try (.getLatestMetadataFor mdf status-for-analysis) (catch Exception _ nil)))]
      (try
        (when meta
          (let [art (.getArtwork meta)]
            (when art
              (javax.imageio.ImageIO/write (.getImage art) "JPEG"
                                          (java.io.File. (str "/tmp/vp_art_P" dev-num ".jpg"))))))
        (catch Exception _))
      (let [tid  (try (long (.getRekordboxId beat)) (catch Exception _ -1))
            titl (vp-esc (when meta (str (.getTitle  meta))))
            art  (vp-esc (vp-si->str (when meta (.getArtist  meta))))
            alb  (vp-esc (vp-si->str (try (when meta (.getAlbum   meta)) (catch Exception _ nil))))
            lbl  (vp-esc (vp-si->str (try (when meta (.getLabel   meta)) (catch Exception _ nil))))
            cmt  (vp-esc (vp-si->str (try (when meta (.getComment meta)) (catch Exception _ nil))))
            gen  (vp-esc (vp-si->str (try (when meta (.getGenre   meta)) (catch Exception _ nil))))
            bpm  (try (when meta (/ (.doubleValue (.getTempo meta)) 100.0)) (catch Exception _ 0.0))
            key  (vp-esc (vp-si->str (try (when meta (.getKey meta)) (catch Exception _ nil))))
            dur  (try (when meta (* (.doubleValue (.getDuration meta)) 1000.0)) (catch Exception _ 0.0))
            rat  (try (when meta (int (.number (.getRating meta)))) (catch Exception _ 0))
            hex  (try (when meta
                        (let [ci (.getColor meta)]
                          (when ci
                            (let [c (try (.color ci) (catch Exception _ nil))]
                              (when c (format "#%02x%02x%02x" (.getRed c) (.getGreen c) (.getBlue c)))))))
                      (catch Exception _ ""))
            [phrases mood bank] (vp-phrases dev-num status-for-analysis)
            _ (swap! globals assoc-in [:vp-players dev-num :phrases-json] phrases)
            cues (vp-cues meta)
            json (str "{\"type\":\"track_meta\",\"player\":" dev-num
                      ",\"track_id\":" tid
                      ",\"title\":\"" titl "\",\"artist\":\"" art "\""
                      ",\"album\":\"" alb "\",\"label\":\"" lbl "\""
                      ",\"comment\":\"" cmt "\",\"genre\":\"" gen "\""
                      ",\"bpm\":" (or bpm 0.0)
                      ",\"key\":\"" key "\""
                      ",\"duration_ms\":" (or dur 0.0)
                      ",\"rating\":" (or rat 0)
                      ",\"color_hex\":\"" (or hex "") "\""
                      ",\"artwork_path\":\"/tmp/vp_art_P" dev-num ".jpg\""
                      ",\"mood\":\"" mood "\",\"bank\":\"" bank "\""
                      ",\"waveform_url\":\"/wave-detail/" dev-num "?width=500\""
                      ",\"waveform_overview_url\":\"/wave-preview/" dev-num "\""
                      ",\"usb_name\":\"\",\"usb_tracks\":0,\"usb_playlists\":0"
                      ",\"sd_name\":\"\",\"sd_tracks\":0,\"sd_playlists\":0"
                      ",\"phrases\":" phrases ",\"cues\":" cues
                      ;; phrases_pending=true → Python moet blijven pollen (ATF nog niet klaar)
                      ",\"phrases_pending\":" (if (= phrases "[]") "true" "false") "}")]
        (println "[VP] dump key=" key " phrases-len=" (count phrases)
                 " pending=" (= phrases "[]") " meta=" (if meta "OK" "nil"))
        (vp-udp! json)))
    (catch Exception e (println "[VP] dump-meta! err:" (.getMessage e)))))

;; ── Beat handler (devices 1-4 only, ignore mixer) ────────────────────────────

;; Beat shim passes: beat status track-time-reached
;; In Trigger Beat Expressions both beat and status are Beat objects.
;; CdjStatus is fetched separately from VirtualCdj.
(defn vp-beat-entry! [beat status track-pos-ms]
  (try
    (let [dev  (int (.getDeviceNumber beat))
          vcj  (org.deepsymmetry.beatlink.VirtualCdj/getInstance)
          cdj  (when vcj
                 (let [u (try (.getLatestStatusFor vcj dev) (catch Exception _ nil))]
                   (when (instance? org.deepsymmetry.beatlink.CdjStatus u) u)))]
      (when (<= dev 4)
        (let [;; Use getRekordboxId — getTrackSourceID not available in this beat-link build
              cur-id   (long (try (if cdj (.getRekordboxId cdj) -1) (catch Exception _ -1)))
              old-id   (get-in @globals [:vp-players dev :track-id] -1)
              ;; BPM and bar-beat come from the Beat object (always accurate)
              bpm      (double (.getEffectiveTempo beat))
              bar-b    (int (.getBeatWithinBar beat))
              ;; Rest from CdjStatus when available
              pitch    (double (try (if cdj (.getPitchPercent cdj) 0.0) (catch Exception _ 0.0)))
              beat-n   (int    (try (if cdj (.getBeatNumber  cdj) 0)   (catch Exception _ 0)))
              on-air   (int    (if (try (and cdj (.isOnAir   cdj)) (catch Exception _ false)) 1 0))
              playing  (int    (if (try (and cdj (.isPlaying cdj)) (catch Exception _ false)) 1 0))
              master   (int    (if (try (and cdj (.isMaster  cdj)) (catch Exception _ false)) 1 0))
              sync-on  (int    (if (try (and cdj (.isSynced  cdj)) (catch Exception _ false)) 1 0))
              pos      (double (or track-pos-ms 0.0))
              [bass mid treble amp] (vp-energy dev)
              ;; ATF-based phrase (only reliable source — SignatureFinder has no phrase API)
              phrase   (vp-current-phrase dev beat-n (or cdj beat))
              p-idx    (int (or (:idx phrase) 0))
              ;; Use last-known phrase when analysis is temporarily unavailable
              ;; (e.g. Python monitor restart — analysis still in ATF cache but vp-analysis cleared)
              p-kind   (or (:kind phrase)
                           (when-not phrase (get-in @globals [:vp-players dev :last-pk] "")))
              p-disp   (or (:display phrase)
                           (when-not phrase (get-in @globals [:vp-players dev :last-pd] "")))
              p-bip    (int (or (:beat-in-phrase phrase) 0))
              p-sec    (or (:section phrase) "")
              next-pb  (int (or (:beat-end phrase) 0))
              btc      (int (max 0 (- next-pb beat-n)))]

          (when (pos? bpm)
            (swap! globals assoc-in [:vp-players dev :last-bpm] bpm))
          ;; Cache last valid phrase so it survives transient ATF lookup failures
          (when (seq p-kind)
            (swap! globals assoc-in [:vp-players dev :last-pk] p-kind)
            (swap! globals assoc-in [:vp-players dev :last-pd] (or p-disp "")))
          ;; Track beat-number per player for file-based fallback
          (swap! globals assoc-in [:vp-players dev :beat-n] beat-n)
          (swap! globals assoc-in [:vp-players dev :beats-to-next] btc)
          (swap! globals assoc-in [:vp-players dev :on-air]    (= on-air  1))
          (swap! globals assoc-in [:vp-players dev :is-playing] (= playing 1))
          (swap! globals assoc-in [:vp-players dev :is-master]  (= master  1))
          (swap! globals assoc-in [:vp-players dev :pos-ms] pos)

          (when (not= cur-id old-id)
            (swap! globals assoc-in [:vp-players dev :track-id] cur-id)
            (swap! globals assoc-in [:vp-players dev :phrase-idx] -1)
            (swap! globals assoc-in [:vp-players dev :last-dump] 0)
            (swap! globals assoc-in [:vp-players dev :last-pk] "")
            (swap! globals assoc-in [:vp-players dev :last-pd] "")
            (swap! globals assoc-in [:vp-analysis dev] nil)
            (if (pos? cur-id)
              (do (vp-osc! "/blt/track-loading" (int dev) (int cur-id))
                  (future
                    ;; BLT 8.x ATF fetches .PXZ via HTTP (port 12523) internally.
                    ;; Request analysis, then after 3s dump diagnostics and metadata.
                    (try
                      ;; BLT 8.x ATF populates the cache automatically — no explicit request needed.
                      ;; Wait briefly for ATF to finish fetching, then dump metadata.
                      (Thread/sleep 1500)
                      (catch Exception e (println "[VP] ATF wait err:" (.getMessage e))))
                    (vp-dump-meta! dev beat)))
              (vp-osc! "/blt/unload" (int dev))))

          ;; Periodic re-dump — every 1.5s, fully async so beat callback is never blocked.
          (let [last-dump (get-in @globals [:vp-players dev :last-dump] 0)
                now-ms    (System/currentTimeMillis)]
            (when (and (pos? cur-id)
                       (> (- now-ms last-dump) 1500))
              (swap! globals assoc-in [:vp-players dev :last-dump] now-ms)
              (future
                ;; ATF requests are blocking network I/O — MUST NOT run on beat thread.
                (try
                  (let [atf (org.deepsymmetry.beatlink.data.AnalysisTagFinder/getInstance)
                        dr  (vp-data-ref cdj)]
                    (when (and atf dr)
                      (.requestAnalysisTagFrom atf dr ".PXZ" "PSSI")
                      (.requestAnalysisTagFrom atf dr ".EXT" "PSSI")))
                  (catch Exception e (println "[VP] re-req err:" (.getMessage e))))
                (vp-dump-meta! dev beat))))

          (let [old-pidx (get-in @globals [:vp-players dev :phrase-idx] -1)]
            (when (not= p-kind (get-in @globals [:vp-players dev :last-phrase-kind] ""))
              (swap! globals assoc-in [:vp-players dev :last-phrase-kind] p-kind)
              (when (pos? (count p-kind))
                (println "[VP] phrase-change dev=" dev " kind=" p-kind)))
            (when (and phrase (not= p-idx old-pidx))
              (swap! globals assoc-in [:vp-players dev :phrase-idx] p-idx)
              (vp-osc! "/blt/phrase" (int dev) (int p-idx) p-kind p-disp
                       (int (:beat-start phrase)) (int (:beat-end phrase)) p-sec)))

          (vp-osc! "/blt/beat"
                   (int dev) (float bpm) (float pitch) (float pos)
                   (int beat-n) (int bar-b)
                   (int on-air) (int playing) (int master) (int sync-on)
                   (float bass) (float mid) (float treble) (float amp)
                   (int p-idx) p-kind p-disp (int p-bip) p-sec
                   (int next-pb) (int btc))

          ;; Build state JSON then write asynchronously so beat callback never blocks on I/O.
          (try
            (let [pl (:vp-players @globals)
                  entries (map (fn [[d pp]]
                                 (str "\"" (int d) "\":{"
                                      "\"phrase_kind\":\"" (vp-esc (get pp :last-pk "")) "\","
                                      "\"beats_to_next\":" (get pp :beats-to-next 0) ","
                                      "\"beat_number\":"   (get pp :beat-n 0) ","
                                      "\"bpm\":"           (format "%.2f" (double (get pp :last-bpm 0.0))) ","
                                      "\"position_ms\":"   (format "%.0f" (double (get pp :pos-ms 0.0))) ","
                                      "\"on_air\":"        (if (get pp :on-air false) "true" "false") ","
                                      "\"is_playing\":"    (if (get pp :is-playing false) "true" "false") ","
                                      "\"is_master\":"     (if (get pp :is-master false) "true" "false") ","
                                      "\"track_id\":"      (get pp :track-id -1) ","
                                      "\"phrases\":"       (get pp :phrases-json "[]") "}"))
                               pl)]
              (let [json-str (str "{\"players\":{" (clojure.string/join "," entries) "}}")]
                (send-off vp-state-agent (fn [_] (try (spit "/tmp/vp_state.json" json-str) (catch Exception _)) nil))))
            (catch Exception _)))))
    (catch Throwable e (println "[VP] beat err:" (.getClass e) (.getMessage e)))))


(defn vp-update-entry! [status]
  (try
    (when-let [osc (:vp-osc @globals)]
      (let [dev (int (try (.getDeviceNumber status) (catch Exception _ 0)))]
        (when (<= dev 4)
          (let [bpm    (float (try (let [c (get-in @globals [:vp-players dev :last-bpm])]
                                     (if (and c (pos? c)) c (try (.getEffectiveTempo status) (catch Exception _ 0.0))))
                                   (catch Exception _ 0.0)))
                pitch  (float (try (.getPitchPercent status) (catch Exception _ 0.0)))
                pos    (float (try (let [tf   (org.deepsymmetry.beatlink.data.TimeFinder/getInstance)
                                         ;; getTimeFor interpolates position to now (accurate)
                                         tval (.getTimeFor tf (int dev))]
                                     (if (and tval (>= tval 0)) tval 0.0))
                                   (catch Exception _ 0.0)))
                dur    (float (try (let [mdf  (org.deepsymmetry.beatlink.data.MetadataFinder/getInstance)
                                         meta (.getLatestMetadataFor mdf dev)]
                                     (if meta (* (.doubleValue (.getDuration meta)) 1000.0) 0.0))
                                   (catch Exception _ 0.0)))
                remain (float (Math/max 0.0 (- dur pos)))
                on-air (int   (if (try (.isOnAir status) (catch Exception _ false)) 1 0))
                play   (int   (if (try (.isPlaying status) (catch Exception _ false)) 1 0))
                mast   (int   (if (try (.isMaster status) (catch Exception _ false)) 1 0))
                sync   (int   (if (try (.isSynced status) (catch Exception _ false)) 1 0))
                looping (int  (if (try (.isLooping status) (catch Exception _ false)) 1 0))]
            ;; Update is-playing in globals so state file stays accurate when player stops
            ;; (beats stop firing when stopped, so vp-beat-entry! won't update it)
            (swap! globals assoc-in [:vp-players dev :is-playing] (= play 1))
            (overtone.osc/osc-send osc "/blt/status" dev bpm pitch pos remain on-air play mast sync looping)))))
    (catch Throwable _)))

;; Register callables in globals for cross-namespace access (BLT 8.0.0 private namespaces)
(swap! globals assoc :vp-beat-fn! vp-beat-entry!)
(swap! globals assoc :vp-update-fn! vp-update-entry!)
(println "[VP] vp-beat-fn! + vp-update-fn! registered in globals")

;; ── VP Phrase Overlay HTTP server (port 17082) ─────────────────────────────
;; Serves a browser-source overlay showing phrase/beats data from vp_state.json.
;; Open http://127.0.0.1:17082/ in OBS browser source (transparent bg, 420×180).

(def ^:private vp-overlay-html
  "<!DOCTYPE html>
<html>
<head>
<meta charset='utf-8'>
<title>VaultFX Phrases</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:transparent;font-family:'Helvetica Neue',Arial,sans-serif;color:#fff;padding:12px}
#overlay{display:flex;flex-direction:column;gap:6px}
.deck{background:rgba(0,0,0,.65);border-left:4px solid #444;border-radius:4px;padding:8px 12px 8px 10px;position:relative;min-width:220px;display:inline-block}
.deck-label{font-size:10px;text-transform:uppercase;color:#888;letter-spacing:1px;margin-bottom:2px}
.phrase-now{font-size:26px;font-weight:700;text-transform:uppercase;letter-spacing:2px;line-height:1.1}
.phrase-next{font-size:12px;color:#999;margin-top:3px}
.beats{position:absolute;top:10px;right:12px;font-size:22px;font-weight:600;color:#e8d84a;line-height:1}
.b-onair{color:#00d250;font-size:10px;font-weight:700;margin-left:6px}
.b-master{color:#dbb400;font-size:10px;font-weight:700;margin-left:6px}
</style>
</head>
<body>
<div id='overlay'></div>
<script>
const PC={intro:'#4466ff',outro:'#cc4488',chorus:'#ff4444',verse:'#44aa44',bridge:'#aa44cc',up:'#44cccc',down:'#cc7744'};
function col(k){if(!k)return'#aaa';const b=k.split('_')[0];return PC[b]||'#aaa';}
function fmt(k){return k?k.replace(/_/g,' ').replace(/\\b\\w/g,c=>c.toUpperCase()):'-';}
async function tick(){
  try{
    const d=await(await fetch('/state.json?_='+Date.now())).json();
    const pl=d.players||{};
    const ac=Object.entries(pl)
      .filter(([,p])=>p.is_playing&&p.beat_number>0)
      .sort(([,a],[,b])=>a.on_air!==b.on_air?a.on_air?-1:1:a.is_master!==b.is_master?a.is_master?-1:1:b.bpm-a.bpm)
      .slice(0,2);
    const el=document.getElementById('overlay');
    if(!ac.length){el.innerHTML='';return;}
    el.innerHTML=ac.map(([n,p])=>{
      const c=col(p.phrase_kind);
      let nxt='';
      if(p.beat_number>0&&p.phrases){
        for(let i=0;i<p.phrases.length;i++){
          if(p.phrases[i].beat_start<=p.beat_number){if(i+1<p.phrases.length)nxt=p.phrases[i+1].kind||'';}else break;
        }
      }
      const btc=p.beats_to_next||0;
      const bars=btc>0?(btc>=4?Math.floor(btc/4)+'b':btc+''):'';
      const badge=p.on_air?`<span class='b-onair'>ON AIR</span>`:p.is_master?`<span class='b-master'>MASTER</span>`:'';
      return `<div class='deck' style='border-color:${c}'>
        <div class='deck-label'>DECK ${n}${badge}</div>
        <div class='phrase-now' style='color:${c}'>${fmt(p.phrase_kind)}</div>
        ${nxt?`<div class='phrase-next'>next &rsaquo; ${fmt(nxt)}</div>`:''}
        ${bars?`<div class='beats'>${bars}</div>`:''}
      </div>`;
    }).join('');
  }catch(e){}
}
tick();setInterval(tick,400);
</script>
</body>
</html>")

(future
  (Thread/sleep 3500)
  (try
    (let [handler
          (fn [req]
            (condp = (:uri req)
              "/state.json"
              {:status  200
               :headers {"Content-Type"                "application/json"
                         "Access-Control-Allow-Origin" "*"
                         "Cache-Control"               "no-cache, no-store"}
               :body    (try (slurp "/tmp/vp_state.json")
                             (catch Exception _ "{\"players\":{}}"))}
              ;; default — serve overlay page
              {:status  200
               :headers {"Content-Type" "text/html; charset=utf-8"
                         "Cache-Control" "no-cache"}
               :body    vp-overlay-html}))]
      (org.httpkit.server/run-server handler {:port 17082 :thread 2})
      (println "[VP] Phrase overlay at http://127.0.0.1:17082/"))
    (catch Exception e
      (println "[VP] Phrase overlay server err:" (.getMessage e)))))

;; ── One-shot startup bind to the CDJ interface (CDJ hub interface) ───────────────────────
;; Runs ONCE, 5s after BLT loads.
;; If CDJs already found: read-only log only.
;; If CDJs not found: restarts DeviceFinder + VirtualCdj on the CDJ interface once, then stops.
;; Safe to restart VirtualCdj here because no CDJs are connected yet.
(future
  (Thread/sleep 5000)
  (try
    (let [df   (org.deepsymmetry.beatlink.DeviceFinder/getInstance)
          vcdj (org.deepsymmetry.beatlink.VirtualCdj/getInstance)
          known (try (.size (.getKnownDevices df)) (catch Exception _ 0))]
      (if (pos? known)
        (println "[VP] DJ-Link OK:" known "devices found — no bind needed")
        (let [iface (when (seq vp-cdj-iface) (java.net.NetworkInterface/getByName vp-cdj-iface))]
          (if (nil? iface)
            (println "[VP] No CDJs yet and no (valid) vp-cdj-iface set — leaving interface choice to BLT")
            (do
              (println "[VP] No CDJs at 5s — binding to the CDJ interface (one-shot)")
              ;; Stop both — safe since no CDJs are connected yet
              (try (.stop vcdj) (catch Exception e (println "[VP] vcdj stop:" (.getMessage e))))
              (Thread/sleep 300)
              (try (.stop df) (catch Exception e (println "[VP] df stop:" (.getMessage e))))
              (Thread/sleep 500)
              ;; Restart DeviceFinder on the CDJ interface
              (try
                (.start df iface)
                (println "[VP] DeviceFinder on the CDJ interface OK")
                (catch Exception e1
                  (println "[VP] the CDJ interface single-iface failed:" (.getMessage e1))
                  (try (.start df) (println "[VP] DeviceFinder all-ifaces OK")
                       (catch Exception e2 (println "[VP] DeviceFinder restart failed:" (.getMessage e2))))))
              (Thread/sleep 1500)
              ;; Restart VirtualCdj so it picks up the CDJ interface
              (try (.start vcdj) (println "[VP] VirtualCdj restarted on the CDJ interface")
                   (catch Exception e (println "[VP] VirtualCdj restart:" (.getMessage e))))
              (Thread/sleep 3000)
              (let [known2 (try (.size (.getKnownDevices df)) (catch Exception _ 0))]
                (println "[VP] After the CDJ interface bind:" known2 "CDJ(s) found")))))))
    (catch Exception e
      (println "[VP] Startup bind err:" (.getMessage e)))))

