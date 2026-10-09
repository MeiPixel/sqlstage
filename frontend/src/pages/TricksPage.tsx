import { useEffect, useState } from 'react'
import { Card, Typography } from 'antd'
import { tricksMarkdown } from '../api'

export default function TricksPage() {
  const [markdown, setMarkdown] = useState('')
  useEffect(() => {
    void tricksMarkdown().then((data) => setMarkdown(data.markdown))
  }, [])
  return (
    <div>
      <div className="page-head" style={{ marginBottom: 16 }}>
        <Typography.Title level={4} className="page-title">完整 Trick 地图</Typography.Title>
        <Typography.Paragraph className="page-desc">
          每项说明包含它解决的问题、适用场景和主要代价。
        </Typography.Paragraph>
      </div>
      <Card>
        <pre className="code" style={{ maxHeight: 'none' }}>{markdown}</pre>
      </Card>
    </div>
  )
}
