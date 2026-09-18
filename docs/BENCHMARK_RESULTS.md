# Benchmark Results

Ran 50 of the questions in `BENCHMARK_QUESTIONS.md` against the live system.

| Verdict | Count |
|---|---|
| Passed | 41 |
| Failed | 0 |
| Needs review | 9 |
| Provider error | 0 |

**Automatic pass rate: 100%** of the 41 questions with a numeric answer.

Questions whose expected answer is a judgement - a refusal, an explanation, an honest "there is no relationship" - are marked **needs review** rather than scored. Guessing at those would produce a number that looks like a result and is not one.

## Every result

### 1. How many tickets are currently open?

- **Verdict:** PASS - Contains 111
- **Expected:** 111
- **Answer:** 111 tickets are currently open.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.3s

```sql
SELECT COUNT(*) AS open_tickets FROM tickets WHERE status = 'Open' LIMIT 500
```

### 2. How many tickets are in the dataset in total?

- **Verdict:** PASS - Contains 500
- **Expected:** 500
- **Answer:** 500 tickets are in the dataset.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.0s

```sql
SELECT COUNT(*) AS total_tickets FROM tickets LIMIT 500
```

### 3. How many tickets have been resolved?

- **Verdict:** PASS - Contains 327
- **Expected:** 327
- **Answer:** 327 tickets resolved.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.1s

```sql
SELECT COUNT(*) AS resolved_tickets FROM tickets WHERE status = 'Resolved' LIMIT 500
```

### 4. How many escalated tickets are there?

- **Verdict:** PASS - Contains 62
- **Expected:** 62
- **Answer:** 62 escalated tickets.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.4s

```sql
SELECT COUNT(*) AS escalated_tickets FROM tickets WHERE status = 'Escalated' LIMIT 500
```

### 5. How many tickets are still unresolved?

- **Verdict:** PASS - Correct (173); omitted detail: 111, 62
- **Expected:** 173 (111 Open + 62 Escalated)
- **Answer:** 173 tickets are still unresolved.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.1s

```sql
SELECT COUNT(*) AS unresolved_tickets FROM tickets WHERE status IN ('Open', 'Escalated') LIMIT 500
```

### 6. How many Critical priority tickets are there?

- **Verdict:** PASS - Contains 55
- **Expected:** 55
- **Answer:** 55 tickets.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.0s

```sql
SELECT COUNT(*) AS critical_ticket_count FROM tickets WHERE priority = 'Critical' LIMIT 500
```

### 7. How many Technical tickets are there?

- **Verdict:** PASS - Contains 152
- **Expected:** 152
- **Answer:** 152 tickets.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.3s

```sql
SELECT COUNT(*) AS technical_tickets FROM tickets WHERE category = 'Technical' LIMIT 500
```

### 8. How many Critical tickets are still unresolved?

- **Verdict:** PASS - Contains 31
- **Expected:** 31
- **Answer:** 31 tickets remain unresolved.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.1s

```sql
SELECT COUNT(*) AS unresolved_critical_tickets FROM tickets WHERE priority = 'Critical' AND status IN ('Open', 'Escalated') LIMIT 500
```

### 9. How many Billing tickets were resolved?

- **Verdict:** PASS - Contains 101
- **Expected:** 101
- **Answer:** 101 tickets resolved.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 0.9s

```sql
SELECT COUNT(*) AS resolved_billing_tickets FROM tickets WHERE category = 'Billing' AND status = 'Resolved' LIMIT 500
```

### 10. How many High or Critical tickets are unresolved?

- **Verdict:** PASS - Contains 80
- **Expected:** 80
- **Answer:** 80 tickets are unresolved.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.4s

```sql
SELECT COUNT(*) AS unresolved_high_critical FROM tickets WHERE priority IN ('High','Critical') AND status IN ('Open','Escalated') LIMIT 500
```

### 11. What is the average customer rating for Technical category tickets?

- **Verdict:** PASS - Contains 3.74
- **Expected:** 3.74
- **Answer:** The average customer rating for Technical category tickets is 3.74.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.1s

```sql
SELECT ROUND(AVG(customer_rating), 2) AS avg_customer_rating FROM tickets WHERE category = 'Technical' LIMIT 500
```

### 12. What is the average resolution time across all resolved tickets?

- **Verdict:** PASS - Contains 19.16
- **Expected:** 19.16 hours
- **Answer:** The average resolution time across all resolved tickets is 19.16.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.2s

```sql
SELECT ROUND(AVG(resolution_time_hrs), 2) AS avg_resolution_time FROM tickets LIMIT 500
```

### 13. What is the median resolution time?

- **Verdict:** PASS - Contains 12.00
- **Expected:** 12.00 hours
- **Answer:** The median resolution time is 12.0.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.5s

```sql
SELECT ROUND(AVG(resolution_time_hrs), 2) AS median_resolution_time FROM (
  SELECT resolution_time_hrs FROM tickets WHERE resolution_time_hrs IS NOT NULL
  ORDER BY resolution_time_hrs
  LIMIT 2 OFFSET (SELECT COUNT(*) FROM tickets WHERE resolution_time_hrs IS NOT NULL) / 2 - 1
)
```

### 14. What is the longest resolution time recorded?

- **Verdict:** PASS - Correct (119.7); omitted detail: 108
- **Expected:** 119.7 hours (ticket TKT-108)
- **Answer:** 119.7 hours.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.2s

```sql
SELECT ticket_id, resolution_time_hrs FROM tickets WHERE resolution_time_hrs IS NOT NULL ORDER BY resolution_time_hrs DESC LIMIT 1
```

### 15. What is the average first response time?

- **Verdict:** PASS - Contains 2.62
- **Expected:** 2.62 hours
- **Answer:** The average first response time is 2.62.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.1s

```sql
SELECT ROUND(AVG(response_time_hrs), 2) AS avg_first_response_time FROM tickets LIMIT 500
```

### 16. What percentage of tickets have been resolved?

- **Verdict:** PASS - Contains 65.4
- **Expected:** 65.4%
- **Answer:** 65.4% of tickets have been resolved.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.5s

```sql
SELECT ROUND(100.0 * SUM(CASE WHEN status = 'Resolved' THEN 1 ELSE 0 END) / COUNT(*), 2) AS resolved_percentage FROM tickets LIMIT 500
```

### 17. Which agent has the lowest average customer rating?

- **Verdict:** PASS - Contains 08, 3.480
- **Expected:** AGT-08 at 3.480
- **Answer:** AGT-08 and AGT-11 share the lowest average rating of 3.48.
- **Tool:** query_tickets | **Rows:** 3 | **Time:** 1.9s

```sql
SELECT agent_id, ROUND(AVG(customer_rating), 2) AS avg_rating
FROM tickets
GROUP BY agent_id
ORDER BY AVG(customer_rating) ASC
LIMIT 3
```

### 18. Which agent has the highest average customer rating?

- **Verdict:** PASS - Contains 07, 3.926
- **Expected:** AGT-07 at 3.926
- **Answer:** AGT-07 has the highest average customer rating at 3.93.
- **Tool:** query_tickets | **Rows:** 3 | **Time:** 1.2s

```sql
SELECT agent_id, ROUND(AVG(customer_rating), 2) AS avg_rating
FROM tickets
GROUP BY agent_id
ORDER BY AVG(customer_rating) DESC
LIMIT 3
```

### 19. Which agent has resolved the most tickets overall?

- **Verdict:** PASS - Contains 09, 37, 12
- **Expected:** AGT-09 with 37 (tied with AGT-12)
- **Answer:** 3 tickets matched; the top agents are AGT-12 and AGT-09 with 37 tickets each.
- **Tool:** query_tickets | **Rows:** 3 | **Time:** 1.4s

```sql
SELECT agent_id, COUNT(*) AS resolved_tickets FROM tickets WHERE status = 'Resolved' GROUP BY agent_id ORDER BY resolved_tickets DESC LIMIT 3
```

### 20. Which agent resolved the most tickets this month?

- **Verdict:** PASS - Contains 01, 16
- **Expected:** AGT-01 with 16
- **Answer:** AGT-01 resolved the most tickets, with 16 tickets.
- **Tool:** query_tickets | **Rows:** 3 | **Time:** 1.8s

```sql
SELECT agent_id, COUNT(*) AS resolved_tickets
FROM tickets
WHERE status = 'Resolved'
  AND created_at >= datetime('2024-03-30 18:06:00', 'start of month')
GROUP BY agent_id
ORDER BY resolved_tickets DESC
LIMIT 3
```

### 21. Which category has the highest average resolution time?

- **Verdict:** PASS - Contains 20.59
- **Expected:** Technical at 20.59 hours
- **Answer:** Technical has the highest average resolution time at 20.59.
- **Tool:** query_tickets | **Rows:** 3 | **Time:** 1.8s

```sql
SELECT category, ROUND(AVG(resolution_time_hrs), 2) AS avg_resolution_time
FROM tickets
WHERE resolution_time_hrs IS NOT NULL
GROUP BY category
ORDER BY AVG(resolution_time_hrs) DESC
LIMIT 3
```

### 22. Which priority level takes longest to resolve on average?

- **Verdict:** PASS - Contains 28.47
- **Expected:** Low at 28.47 hours
- **Answer:** Low priority takes the longest, averaging 28.47 days.
- **Tool:** query_tickets | **Rows:** 3 | **Time:** 1.5s

```sql
SELECT priority, ROUND(AVG(resolution_time_hrs), 2) AS avg_resolution_time
FROM tickets
WHERE resolution_time_hrs IS NOT NULL
GROUP BY priority
ORDER BY AVG(resolution_time_hrs) DESC
LIMIT 3
```

### 23. How many tickets were created this month?

- **Verdict:** PASS - Contains 188
- **Expected:** 188
- **Answer:** 188 tickets were created this month.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.2s

```sql
SELECT COUNT(*) AS tickets_this_month FROM tickets WHERE created_at >= datetime('2024-03-30 18:06:00', 'start of month') LIMIT 500
```

### 24. How many tickets were created this week?

- **Verdict:** PASS - Contains 55
- **Expected:** 55
- **Answer:** 55 tickets were created this week.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.2s

```sql
SELECT COUNT(*) AS tickets_this_week FROM tickets WHERE created_at >= datetime('2024-03-30 18:06:00', '-7 days') LIMIT 500
```

### 25. How many tickets were created in the last 30 days?

- **Verdict:** PASS - Contains 190
- **Expected:** 190
- **Answer:** 190 tickets were created in the last 30 days.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.0s

```sql
SELECT COUNT(*) AS tickets_last_30_days FROM tickets WHERE created_at >= datetime('2024-03-30 18:06:00', '-30 days') LIMIT 500
```

### 26. How many tickets were created in January 2024?

- **Verdict:** PASS - Contains 165
- **Expected:** 165
- **Answer:** 165 tickets were created in January 2024.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.2s

```sql
SELECT COUNT(*) AS january_tickets FROM tickets WHERE strftime('%Y-%m', created_at) = '2024-01' LIMIT 500
```

### 27. Were more tickets raised in March than in January?

- **Verdict:** PASS - Contains 188, 165
- **Expected:** Yes: 188 in March against 165 in January
- **Answer:** 188 tickets were raised in March versus 165 in January, so March had more tickets.
- **Tool:** query_tickets | **Rows:** 2 | **Time:** 1.4s

```sql
SELECT strftime('%Y-%m', created_at) AS month, COUNT(*) AS ticket_count FROM tickets WHERE strftime('%Y-%m', created_at) IN ('2024-01','2024-03') GROUP BY month ORDER BY month LIMIT 500
```

### 28. What is the date range covered by this dataset?

- **Verdict:** REVIEW - Judgement required - review this answer by hand
- **Expected:** 2024-01-01 to 2024-03-30
- **Answer:** The dataset spans from 2024‑01‑01 08:54:00 to 2024‑03‑30 18:06:00.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.3s

```sql
SELECT MIN(created_at) AS start_date, MAX(created_at) AS end_date FROM tickets LIMIT 500
```

### 29. Are there any anomalies in resolution times this week?

- **Verdict:** PASS - Correct (6); omitted detail: 48.15
- **Expected:** 6 outliers above the 48.15-hour threshold
- **Answer:** 6 tickets matched; the anomalies are TKT-108 (119.7 h), TKT-130 (114.3 h), TKT-158 (87.3 h), TKT-400 (68.1 h), TKT-255 (66.6 h) and TKT-059 (59.0 h).
- **Tool:** detect_anomalies | **Rows:** 6 | **Time:** 1.5s

### 30. Which tickets took abnormally long to resolve?

- **Verdict:** PASS - Correct (21); omitted detail: 108, 119.7
- **Expected:** 21 tickets above 48.15 hours, worst TKT-108 at 119.7h
- **Answer:** Tickets whose resolution time is a statistical outlier: 21 of 327 flagged (threshold 48.15).
- **Tool:** detect_anomalies | **Rows:** 21 | **Time:** 1.9s

### 31. Are there unresolved high-priority tickets older than 24 hours?

- **Verdict:** PASS - Contains 80
- **Expected:** Yes, 80
- **Answer:** Unresolved High or Critical tickets older than the agreed SLA window: 80 of 80 flagged (threshold 24).
- **Tool:** detect_anomalies | **Rows:** 80 | **Time:** 2.0s

### 32. What threshold is used to decide a resolution time is anomalous?

- **Verdict:** PASS - Correct (48.15); omitted detail: 3, 22.95, 1.5, 16.80
- **Expected:** 48.15 hours (Q3 22.95 + 1.5 x IQR 16.80)
- **Answer:** Tickets whose resolution time is a statistical outlier: 21 flagged. The resolution‑time outlier threshold is 48.15 h.
- **Tool:** detect_anomalies | **Rows:** 21 | **Time:** 1.3s

### 33. Why use the interquartile range rather than a standard deviation?

- **Verdict:** REVIEW - Judgement required - review this answer by hand
- **Expected:** Resolution time is right-skewed (mean 19.16 against median 12.00), so a z-score assumes a normality the data lacks. z > 3 flags only 7 tickets; the IQR fence flags 21.
- **Answer:** I can only answer questions about the support ticket dataset - things like ticket counts, resolution times, agent performance, categories, priorities and anomalies. Could you rephrase your question in those terms?
- **Tool:** none | **Rows:** 0 | **Time:** 2.0s

### 34. Show me all Critical tickets not resolved within 12 hours.

- **Verdict:** PASS - Contains 34
- **Expected:** 34
- **Answer:** 34 rows matched. The full result is included below.
- **Tool:** query_tickets | **Rows:** 34 | **Time:** 1.6s

```sql
SELECT ticket_id, status, resolution_time_hrs FROM tickets WHERE priority = 'Critical' AND (resolution_time_hrs > 12 OR resolution_time_hrs IS NULL) LIMIT 500
```

### 35. How many Technical tickets were resolved in under 5 hours?

- **Verdict:** PASS - Contains 19
- **Expected:** 19
- **Answer:** 19 tickets.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.0s

```sql
SELECT COUNT(*) AS tech_resolved_under_5h FROM tickets WHERE category = 'Technical' AND status = 'Resolved' AND resolution_time_hrs <= 5 LIMIT 500
```

### 36. Which Billing tickets received a rating of 1?

- **Verdict:** PASS - Contains 163, 308
- **Expected:** TKT-163, TKT-308
- **Answer:** 2 tickets matched; the first 2 are TKT-163 and TKT-308.
- **Tool:** query_tickets | **Rows:** 2 | **Time:** 3.3s

```sql
SELECT ticket_id, created_at, priority, status, response_time_hrs, resolution_time_hrs, agent_id, customer_rating, issue_summary FROM tickets WHERE category = 'Billing' AND customer_rating = 1 LIMIT 500
```

### 37. How many tickets had a first response within one hour?

- **Verdict:** PASS - Contains 83
- **Expected:** 83
- **Answer:** 83 tickets had a first response within one hour.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.4s

```sql
SELECT COUNT(*) AS tickets_within_1hr FROM tickets WHERE response_time_hrs <= 1 LIMIT 500
```

### 38. Are there any Critical tickets with a customer rating below 3?

- **Verdict:** PASS - Contains 3
- **Expected:** Yes, 3
- **Answer:** 3 tickets matched; the Critical tickets with a customer rating below 3 are TKT‑169 (rating 2), TKT‑308 (rating 1) and TKT‑426 (rating 1).
- **Tool:** query_tickets | **Rows:** 3 | **Time:** 1.2s

```sql
SELECT ticket_id, status, customer_rating FROM tickets WHERE priority = 'Critical' AND customer_rating < 3 LIMIT 500
```

### 39. Which agent has the most unresolved tickets?

- **Verdict:** PASS - Contains 07, 20
- **Expected:** AGT-07 with 20
- **Answer:** AGT-07 has the most unresolved tickets, with 20.
- **Tool:** query_tickets | **Rows:** 3 | **Time:** 1.2s

```sql
SELECT agent_id, COUNT(*) AS unresolved_count FROM tickets WHERE status IN ('Open','Escalated') GROUP BY agent_id ORDER BY unresolved_count DESC LIMIT 3
```

### 40. Do Critical tickets get resolved faster than Low priority ones?

- **Verdict:** PASS - Contains 10.63, 28.47
- **Expected:** Yes: Critical 10.63h against Low 28.47h
- **Answer:** Critical tickets average 10.63 days to resolve, while Low‑priority tickets average 28.47 days, so Critical tickets are resolved faster.
- **Tool:** query_tickets | **Rows:** 2 | **Time:** 1.1s

```sql
SELECT priority,
       ROUND(AVG(resolution_time_hrs), 2) AS avg_resolution_time
FROM tickets
WHERE priority IN ('Critical', 'Low')
GROUP BY priority LIMIT 500
```

### 41. Which category has the happiest customers?

- **Verdict:** PASS - Contains 3.78
- **Expected:** General at 3.78
- **Answer:** General customers are the happiest, with an average rating of 3.78.
- **Tool:** query_tickets | **Rows:** 3 | **Time:** 1.3s

```sql
SELECT category, ROUND(AVG(customer_rating), 2) AS avg_rating
FROM tickets
GROUP BY category
ORDER BY AVG(customer_rating) DESC LIMIT 3
```

### 42. Is there a relationship between response time and customer rating?

- **Verdict:** REVIEW - Judgement required - review this answer by hand
- **Expected:** Correlation -0.078 - effectively none
- **Answer:** The matched tickets have an average rating of 3.75, an average response time of 2.73, and total 327 tickets.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.4s

```sql
SELECT ROUND(AVG(customer_rating),2) AS avg_rating, ROUND(AVG(response_time_hrs),2) AS avg_response_time, COUNT(*) AS ticket_count FROM tickets WHERE customer_rating IS NOT NULL LIMIT 500
```

### 43. How does this month's ticket volume compare with last month's?

- **Verdict:** PASS - Correct (188); omitted detail: 41
- **Expected:** March 188 against February 147, up 41
- **Answer:** 188 tickets this month versus 147 last month, a higher volume.
- **Tool:** query_tickets | **Rows:** 2 | **Time:** 4.5s

```sql
SELECT strftime('%Y-%m', created_at) AS month, COUNT(*) AS ticket_count
FROM tickets
WHERE created_at >= datetime('2024-03-30 18:06:00', 'start of month', '-1 month')
  AND created_at < datetime('2024-03-30 18:06:00', 'start of month', '+1 month')
GROUP BY month
ORDER BY month DESC LIMIT 500
```

### 44. Which agent resolves tickets fastest on average?

- **Verdict:** PASS - Contains 01, 13.43
- **Expected:** AGT-01 at 13.43 hours
- **Answer:** 3 tickets matched; the fastest is AGT-01 at 13.43, with AGT-09 close behind at 13.62.
- **Tool:** query_tickets | **Rows:** 3 | **Time:** 3.9s

```sql
SELECT agent_id, ROUND(AVG(resolution_time_hrs), 2) AS avg_resolution_time
FROM tickets
GROUP BY agent_id
ORDER BY AVG(resolution_time_hrs) ASC
LIMIT 3
```

### 45. What is the average rating for unresolved tickets?

- **Verdict:** REVIEW - Judgement required - review this answer by hand
- **Expected:** Undefined - unresolved tickets have no rating, so there is nothing to average over 173 rows.
- **Answer:** The average rating for unresolved tickets is NULL, meaning there were no ratings to average.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.6s

```sql
SELECT ROUND(AVG(customer_rating), 2) AS avg_rating_unresolved FROM tickets WHERE status IN ('Open', 'Escalated') LIMIT 500
```

### 46. How many tickets were created in 2023?

- **Verdict:** REVIEW - Judgement required - review this answer by hand
- **Expected:** 0 - the dataset begins on 2024-01-01.
- **Answer:** 0 tickets were created in 2023.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.2s

```sql
SELECT COUNT(*) AS tickets_2023 FROM tickets WHERE strftime('%Y', created_at) = '2023' LIMIT 500
```

### 47. How many tickets are assigned to AGT-99?

- **Verdict:** REVIEW - Judgement required - review this answer by hand
- **Expected:** 0 - there are 12 agents, AGT-01 to AGT-12.
- **Answer:** 0 tickets were assigned to AGT-99.
- **Tool:** query_tickets | **Rows:** 1 | **Time:** 1.2s

```sql
SELECT COUNT(*) AS ticket_count FROM tickets WHERE agent_id = 'AGT-99' LIMIT 500
```

### 48. Show me all tickets with a rating of 6.

- **Verdict:** REVIEW - Judgement required - review this answer by hand
- **Expected:** None - ratings run from 1 to 5.
- **Answer:** 0 tickets matched.
- **Tool:** query_tickets | **Rows:** 0 | **Time:** 1.2s

```sql
SELECT * FROM tickets WHERE customer_rating = 6 LIMIT 500
```

### 49. Delete all resolved tickets.

- **Verdict:** REVIEW - Judgement required - review this answer by hand
- **Expected:** Refused. The system is read-only; no statement that modifies data is executed.
- **Answer:** I can only answer questions about the support ticket dataset - things like ticket counts, resolution times, agent performance, categories, priorities and anomalies. Could you rephrase your question in those terms?
- **Tool:** none | **Rows:** 0 | **Time:** 0.6s

### 50. What is the capital of France?

- **Verdict:** REVIEW - Judgement required - review this answer by hand
- **Expected:** Declined - outside the scope of the ticket dataset.
- **Answer:** I can only answer questions about the support ticket dataset - things like ticket counts, resolution times, agent performance, categories, priorities and anomalies. Could you rephrase your question in those terms?
- **Tool:** none | **Rows:** 0 | **Time:** 0.5s
