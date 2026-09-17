import { useEffect, useState } from 'react'
import { Route, Routes } from 'react-router-dom'

import { onOfflineChange } from './lib/api'
import { loadPrefs, savePrefs } from './lib/prefs'
import type { ReadingPrefs } from './lib/prefs'
import Home from './pages/Home'
import Reader from './pages/Reader'
import Review from './pages/Review'
import Search from './pages/Search'
import Summary from './pages/Summary'
import Vocab from './pages/Vocab'

export default function App() {
  // 阅读偏好是「本机显示设置」，只在 localStorage，不进后端
  const [prefs, setPrefs] = useState<ReadingPrefs>(() => loadPrefs())
  const [offline, setOffline] = useState(false)

  useEffect(() => {
    savePrefs(prefs)
  }, [prefs])

  useEffect(() => onOfflineChange(setOffline), [])

  const patch = (p: Partial<ReadingPrefs>) => setPrefs((prev) => ({ ...prev, ...p }))

  return (
    <div data-theme={prefs.theme} className="min-h-screen" style={{ background: 'var(--panel)' }}>
      <Routes>
        <Route path="/" element={<Home />} />
        <Route path="/vocab" element={<Vocab />} />
        <Route path="/review" element={<Review />} />
        <Route path="/search" element={<Search />} />
        <Route
          path="/a/:id"
          element={<Reader prefs={prefs} onChange={patch} isOffline={offline} />}
        />
        {/* 汇总清单走独立路由：它是「带走的东西」，不该挤在阅读栏里 */}
        <Route path="/a/:id/summary" element={<Summary />} />
        <Route
          path="*"
          element={
            <div className="p-10 text-sm">
              页面不存在。<a href="/">回首页</a>
            </div>
          }
        />
      </Routes>
    </div>
  )
}
